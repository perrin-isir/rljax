from functools import partial
from typing import Any, Tuple

import numpy as np

import haiku as hk
import jax
import jax.numpy as jnp
from jax.experimental import optix
from rljax.algorithm.base import OnPolicyActorCritic
from rljax.network.actor import StateIndependentGaussianPolicy
from rljax.network.critic import ContinuousVFunction
from rljax.utils import clip_gradient, evaluate_lop_pi, reparameterize


def build_ppo_critic(state_space, hidden_units):
    def _func(x):
        return ContinuousVFunction(
            num_critics=1,
            hidden_units=hidden_units,
            hidden_activation=jnp.tanh,
        )(x)

    fake_input = state_space.sample()
    return hk.transform(_func), fake_input[None, ...].astype(np.float32)


def build_ppo_actor(state_space, action_space, hidden_units):
    def _func(x):
        return StateIndependentGaussianPolicy(
            action_dim=action_space.shape[0],
            hidden_units=hidden_units,
            hidden_activation=jnp.tanh,
        )(x)

    fake_input = state_space.sample()
    return hk.transform(_func), fake_input[None, ...].astype(np.float32)


class PPO(OnPolicyActorCritic):
    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma=0.995,
        buffer_size=2048,
        batch_size=64,
        lr_actor=3e-4,
        lr_critic=3e-4,
        units_actor=(64, 64),
        units_critic=(64, 64),
        epoch_ppo=10,
        clip_eps=0.2,
        lambd=0.97,
        max_grad_norm=10.0,
    ):
        assert buffer_size % batch_size == 0
        super(PPO, self).__init__(
            num_steps=num_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
            buffer_size=buffer_size,
            batch_size=batch_size,
        )

        # Critic.
        self.critic, fake_input = build_ppo_critic(state_space, units_critic)
        opt_init, self.opt_critic = optix.adam(lr_critic)
        self.params_critic = self.params_critic_target = self.critic.init(next(self.rng), fake_input)
        self.opt_state_critic = opt_init(self.params_critic)

        # Actor.
        self.actor, fake_input = build_ppo_actor(state_space, action_space, units_actor)
        opt_init, self.opt_actor = optix.adam(lr_actor)
        self.params_actor = self.params_actor_target = self.actor.init(next(self.rng), fake_input)
        self.opt_state_actor = opt_init(self.params_actor)

        # Other parameters.
        self.epoch_ppo = epoch_ppo
        self.clip_eps = clip_eps
        self.lambd = lambd
        self.max_grad_norm = max_grad_norm
        self.idxes = np.arange(buffer_size)

    @partial(jax.jit, static_argnums=0)
    def _select_action(
        self,
        params_actor: hk.Params,
        state: np.ndarray,
    ) -> jnp.ndarray:
        mean, _ = self.actor.apply(params_actor, None, state)
        return jnp.tanh(mean)

    @partial(jax.jit, static_argnums=0)
    def _explore(
        self,
        params_actor: hk.Params,
        rng: jnp.ndarray,
        state: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        mean, log_std = self.actor.apply(params_actor, None, state)
        return reparameterize(mean, log_std, rng)

    def update(self, writer):
        state, action, reward, done, log_pi_old, next_state = self.buffer.get()

        # Calculate gamma-return and gae.
        target, gae = self.calculate_gae(
            params_critic=self.params_critic,
            state=state,
            reward=reward,
            done=done,
            next_state=next_state,
        )

        for _ in range(self.epoch_ppo):
            np.random.shuffle(self.idxes)
            for start in range(0, self.buffer_size, self.batch_size):
                self.learning_step += 1
                idx = self.idxes[start : start + self.batch_size]

                # Update critic.
                self.opt_state_critic, self.params_critic, loss_critic = self._update_critic(
                    opt_state_critic=self.opt_state_critic,
                    params_critic=self.params_critic,
                    state=state[idx],
                    target=target[idx],
                )

                # Update actor.
                self.opt_state_actor, self.params_actor, loss_actor = self._update_actor(
                    opt_state_actor=self.opt_state_actor,
                    params_actor=self.params_actor,
                    state=state[idx],
                    action=action[idx],
                    log_pi_old=log_pi_old[idx],
                    gae=gae[idx],
                )

        writer.add_scalar("loss/critic", loss_critic, self.learning_step)
        writer.add_scalar("loss/actor", loss_actor, self.learning_step)

    @partial(jax.jit, static_argnums=0)
    def _update_critic(
        self,
        opt_state_critic: Any,
        params_critic: hk.Params,
        state: np.ndarray,
        target: np.ndarray,
    ) -> Tuple[Any, hk.Params, jnp.ndarray, jnp.ndarray]:
        loss_critic, grad_critic = jax.value_and_grad(self._loss_critic)(
            params_critic,
            state=state,
            target=target,
        )
        grad_critic = clip_gradient(grad_critic, self.max_grad_norm)
        update, opt_state_critic = self.opt_critic(grad_critic, opt_state_critic)
        params_critic = optix.apply_updates(params_critic, update)
        return opt_state_critic, params_critic, loss_critic

    @partial(jax.jit, static_argnums=0)
    def _loss_critic(
        self,
        params_critic: hk.Params,
        state: np.ndarray,
        target: np.ndarray,
    ) -> jnp.ndarray:
        return jnp.square(target - self.critic.apply(params_critic, None, state)).mean()

    @partial(jax.jit, static_argnums=0)
    def _update_actor(
        self,
        opt_state_actor: Any,
        params_actor: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        log_pi_old: np.ndarray,
        gae: jnp.ndarray,
    ) -> Tuple[Any, hk.Params, jnp.ndarray]:
        loss_actor, grad_actor = jax.value_and_grad(self._loss_actor)(
            params_actor,
            state=state,
            action=action,
            log_pi_old=log_pi_old,
            gae=gae,
        )
        grad_actor = clip_gradient(grad_actor, self.max_grad_norm)
        update, opt_state_actor = self.opt_actor(grad_actor, opt_state_actor)
        params_actor = optix.apply_updates(params_actor, update)
        return opt_state_actor, params_actor, loss_actor

    @partial(jax.jit, static_argnums=0)
    def _loss_actor(
        self,
        params_actor: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        log_pi_old: np.ndarray,
        gae: jnp.ndarray,
    ) -> jnp.ndarray:
        mean, log_pi = self.actor.apply(params_actor, None, state)
        log_pi = evaluate_lop_pi(mean, log_pi, action)
        ratio = jnp.exp(log_pi - log_pi_old)
        loss_actor1 = -ratio * gae
        loss_actor2 = -jnp.clip(ratio, 1.0 - self.clip_eps, 1.0 + self.clip_eps) * gae
        loss_actor = jnp.maximum(loss_actor1, loss_actor2).mean()
        return loss_actor

    @partial(jax.jit, static_argnums=0)
    def calculate_gae(
        self,
        params_critic: hk.Params,
        state: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        # Current and next value estimates.
        value = jax.lax.stop_gradient(self.critic.apply(params_critic, None, state))
        next_value = jax.lax.stop_gradient(self.critic.apply(params_critic, None, next_state))
        # Calculate TD errors.
        delta = reward + self.gamma * next_value * (1.0 - done) - value
        # Calculate gae recursively from behind.
        gae = [delta[-1]]
        for t in jnp.arange(reward.shape[0] - 2, -1, -1):
            gae.insert(0, delta[t] + self.gamma * self.lambd * (1 - done[t]) * gae[0])
        gae = jnp.array(gae)
        return gae + value, (gae - gae.mean()) / (gae.std() + 1e-8)

    def __str__(self):
        return "PPO"
