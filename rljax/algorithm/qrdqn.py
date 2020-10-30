from functools import partial
from typing import Tuple

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np

from rljax.algorithm.dqn import DQN
from rljax.network import DiscreteQuantileFunction
from rljax.util import get_quantile_at_action, quantile_loss


class QRDQN(DQN):
    name = "QR-DQN"

    def __init__(
        self,
        num_agent_steps,
        state_space,
        action_space,
        seed,
        max_grad_norm=None,
        gamma=0.99,
        nstep=1,
        buffer_size=10 ** 6,
        use_per=False,
        batch_size=32,
        start_steps=50000,
        update_interval=4,
        update_interval_target=8000,
        eps=0.01,
        eps_eval=0.001,
        eps_decay_steps=250000,
        loss_type="huber",
        dueling_net=False,
        double_q=False,
        fn=None,
        lr=5e-5,
        units=(512,),
        num_quantiles=200,
    ):
        if fn is None:

            def fn(s):
                return DiscreteQuantileFunction(
                    action_space=action_space,
                    num_quantiles=num_quantiles,
                    hidden_units=units,
                    dueling_net=dueling_net,
                )(s)

        super(QRDQN, self).__init__(
            num_agent_steps=num_agent_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            max_grad_norm=max_grad_norm,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            batch_size=batch_size,
            use_per=use_per,
            start_steps=start_steps,
            update_interval=update_interval,
            update_interval_target=update_interval_target,
            eps=eps,
            eps_eval=eps_eval,
            eps_decay_steps=eps_decay_steps,
            loss_type=loss_type,
            dueling_net=dueling_net,
            double_q=double_q,
            fn=fn,
            lr=lr,
        )
        self.num_quantiles = num_quantiles

    @partial(jax.jit, static_argnums=0)
    def _calculate_q_s(
        self,
        params: hk.Params,
        state: np.ndarray,
    ) -> jnp.ndarray:
        return self.net.apply(params, state).mean(axis=1)

    @partial(jax.jit, static_argnums=0)
    def _calculate_quantile(
        self,
        params: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        **kwargs,
    ) -> jnp.ndarray:
        return get_quantile_at_action(self.net.apply(params, state, **kwargs), action)

    @partial(jax.jit, static_argnums=0)
    def _calculate_target(
        self,
        params: hk.Params,
        params_target: hk.Params,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        **kwargs,
    ) -> jnp.ndarray:
        if self.double_q:
            next_action = self._forward(params=params, state=next_state, **kwargs)[..., None]
            next_quantile = self._calculate_quantile(params_target, next_state, next_action)
        else:
            next_quantile = jnp.max(self.net.apply(params_target, next_state), axis=-1, keepdims=True)
        target = reward[:, None] + (1.0 - done[:, None]) * self.discount * next_quantile
        return jax.lax.stop_gradient(target).reshape(-1, 1, self.num_quantiles)

    @partial(jax.jit, static_argnums=0)
    def _calculate_loss_and_abs_td(
        self,
        quantile: jnp.ndarray,
        target: jnp.ndarray,
        cum_p: jnp.ndarray,
        weight: np.ndarray,
    ) -> jnp.ndarray:
        td = target - quantile
        loss = quantile_loss(td, cum_p, weight, self.loss_type)
        return loss, jax.lax.stop_gradient(jnp.abs(td).sum(axis=1).mean(axis=1, keepdims=True))

    @partial(jax.jit, static_argnums=0)
    def _loss(
        self,
        params: hk.Params,
        params_target: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        weight: np.ndarray,
        **kwargs,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        cum_p = (jnp.arange(0, self.num_quantiles, dtype=jnp.float32)[None, :] + 0.5) / self.num_quantiles
        quantile = self._calculate_quantile(params, state, action)
        target = self._calculate_target(params, params_target, reward, done, next_state)
        return self._calculate_loss_and_abs_td(quantile, target, cum_p, weight)
