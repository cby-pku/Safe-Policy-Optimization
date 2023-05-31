# Copyright 2023 OmniSafeAI Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


from __future__ import annotations

import argparse
import random
import time

from collections import deque
from distutils.util import strtobool
from safety_gymnasium.wrappers import SafeAutoResetWrapper, SafeNormalizeObservation, SafeUnsqueeze, SafeRescaleAction
import numpy as np
import safety_gymnasium
import torch
import torch.optim
from rich.progress import track
from typing import Callable

import torch.nn as nn
from torch.distributions import Normal
from safepo.common.logger import EpochLogger
from torch.utils.data import DataLoader, TensorDataset


def parse_args():
    # training parameters
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0,
        help="seed of the experiment")
    parser.add_argument("--device", type=str, default="cpu",
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--torch-threads", type=int, default=1,
        help="number of threads for torch")
    parser.add_argument("--num-envs", type=int, default=1,
        help="the number of parallel game environments")
    parser.add_argument("--total-steps", type=int, default=1024000,
        help="total timesteps of the experiments")
    parser.add_argument("--env-id", type=str, default="SafetyPointGoal1-v0",
        help="the id of the environment")
    # general algorithm parameters
    parser.add_argument("--steps_per_epoch", type=int, default=2048,
        help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--update-iters", type=int, default=40,
        help="the max iteration to update the policy")
    parser.add_argument("--batch-size", type=int, default=64,
        help="the number of mini-batches")
    parser.add_argument("--entropy_coef", type=float, default=0.0,
        help="coefficient of the entropy")
    parser.add_argument("--target-kl", type=float, default=0.02,
        help="the target KL divergence threshold")
    parser.add_argument("--max-grad-norm", type=float, default=40.0,
        help="the maximum norm for the gradient clipping")
    parser.add_argument("--critic-norm-coef", type=float, default=0.001,
        help="the critic norm coefficient")
    parser.add_argument("--gamma", type=float, default=0.99,
        help="the discount factor gamma")
    parser.add_argument("--lam", type=float, default=0.95,
        help="the lambda for the reward general advantage estimation")
    parser.add_argument("--lam-c", type=float, default=0.95,
        help="the lambda for the cost general advantage estimation")
    parser.add_argument("--standardized_adv_r", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="toggles reward advantages standardization")
    parser.add_argument("--standardized_adv_c", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="toggles cost advantages standardization")
    parser.add_argument("--actor_lr", type=float, default=3e-4,
        help="the learning rate of the actor network")
    parser.add_argument("--critic_lr", type=float, default=3e-4,
        help="the learning rate of the critic network")
    parser.add_argument("--linear-lr-decay", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="toggles learning rate annealing for policy and value networks")
    # logger parameters
    parser.add_argument("--log-dir", type=str, default="../runs",
        help="directory to save agent logs (default: ../runs)")
    parser.add_argument("--use-tensorboard", type=lambda x: bool(strtobool(x)), default=False,
        help="toggles tensorboard logging")
    # algorithm specific parameters
    parser.add_argument("--fvp-sample-freq", type=int, default=1,
        help="the sub-sampling rate of the observation")
    parser.add_argument("--cg-damping", type=float, default=0.1,
        help="the damping value for conjugate gradient")
    parser.add_argument("--cg-iters", type=int, default=15,
        help="the number of conjugate gradient iterations")
    args = parser.parse_args()
    return args

def build_mlp_network(sizes):
    layers = list()
    for j in range(len(sizes) - 1):
        act = nn.Tanh if j < len(sizes) - 2 else nn.Identity
        affine_layer = nn.Linear(sizes[j], sizes[j + 1])
        nn.init.kaiming_uniform_(affine_layer.weight, a=np.sqrt(5))
        layers += [affine_layer, act()]
    return nn.Sequential(*layers)

class Actor(nn.Module):
    """Actor network."""
    def __init__(self, obs_dim: int, act_dim: int):
        super().__init__()
        self.mean = build_mlp_network([obs_dim, 64, 64, act_dim])
        self.log_std = nn.Parameter(torch.zeros(act_dim), requires_grad=True)

    def forward(self, obs: torch.Tensor):
        mean = self.mean(obs)
        std = torch.exp(self.log_std)
        return Normal(mean, std)

class Critic(nn.Module):
    """Critic network."""
    def __init__(self, obs_dim):
        super().__init__()
        self.critic = build_mlp_network([obs_dim, 64, 64, 1])

    def forward(self, obs):
        return torch.squeeze(self.critic(obs), -1)

class Policy(nn.Module):
    """Actor critic policy."""
    def __init__(self, obs_dim, act_dim):
        super().__init__()
        self.reward_critic = Critic(obs_dim)
        self.cost_critic = Critic(obs_dim)
        self.actor = Actor(obs_dim, act_dim)

    def get_value(self, obs):
        return self.critic(obs)

    def step(self, obs, deterministic=False):
        dist = self.actor(obs)
        if deterministic:
            action = dist.mean
        else:
            action = dist.rsample()
        log_prob = dist.log_prob(action).sum(axis=-1)
        value_r = self.reward_critic(obs)
        value_c = self.cost_critic(obs)
        return action, log_prob, value_r, value_c

def discount_cumsum(vector_x: torch.Tensor, discount: float) -> torch.Tensor:
    length = vector_x.shape[0]
    vector_x = vector_x.type(torch.float64)
    cumsum = vector_x[-1]
    for idx in reversed(range(length - 1)):
        cumsum = vector_x[idx] + discount * cumsum
        vector_x[idx] = cumsum
    return vector_x

def calculate_adv_and_value_targets(
    values: torch.Tensor,
    rewards: torch.Tensor,
    lam: float,
    gamma: float,
) -> tuple[torch.Tensor, torch.Tensor]:
    # GAE formula: A_t = \sum_{k=0}^{n-1} (lam*gamma)^k delta_{t+k}
    deltas = rewards[:-1] + gamma * values[1:] - values[:-1]
    adv = discount_cumsum(deltas, gamma * lam)
    target_value = adv + values[:-1]
    return adv, target_value

class VectorizedBuffer:

    def __init__(  # pylint: disable=too-many-arguments
        self,
        obs_space,
        act_space,
        size: int,
        gamma: float,
        lam: float,
        lam_c: float,
        standardized_adv_r: bool = True,
        standardized_adv_c: bool = True,
        device: torch.device = 'cpu',
        num_envs: int = 1,
    ) -> None:
        self.buffers: list[dict[str, torch.tensor]] = [
            {
                'obs': torch.zeros((size, *obs_space.shape), dtype=torch.float32, device=device),
                'act': torch.zeros((size, *act_space.shape), dtype=torch.float32, device=device),
                'reward': torch.zeros(size, dtype=torch.float32, device=device),
                'cost': torch.zeros(size, dtype=torch.float32, device=device),
                'done': torch.zeros(size, dtype=torch.float32, device=device),
                'value_r': torch.zeros(size, dtype=torch.float32, device=device),
                'value_c': torch.zeros(size, dtype=torch.float32, device=device),
                'adv_r': torch.zeros(size, dtype=torch.float32, device=device),
                'adv_c': torch.zeros(size, dtype=torch.float32, device=device),
                'target_value_r': torch.zeros(size, dtype=torch.float32, device=device),
                'target_value_c': torch.zeros(size, dtype=torch.float32, device=device),
                'log_prob': torch.zeros(size, dtype=torch.float32, device=device),
            }
            for _ in range(num_envs)
        ]
        self._gamma = gamma
        self._lam = lam
        self._lam_c = lam_c
        self._standardized_adv_r = standardized_adv_r
        self._standardized_adv_c = standardized_adv_c
        self.ptr_list = [0] * num_envs
        self.path_start_idx_list = [0] * num_envs
        self._device = device
        self.num_envs = num_envs
        
    def store(self, **data: torch.Tensor) -> None:
        """Store vectorized data into vectorized buffer."""
        for i, buffer in enumerate(self.buffers):
            assert self.ptr_list[i] < buffer['obs'].shape[0], 'Buffer overflow'
            for key, value in data.items():
                buffer[key][self.ptr_list[i]] = value[i]
            self.ptr_list[i] += 1

    def finish_path(
        self,
        last_value_r: torch.Tensor | None = None,
        last_value_c: torch.Tensor | None = None,
        idx: int = 0,
    ) -> None:
        if last_value_r is None:
            last_value_r = torch.zeros(1, device=self._device)
        if last_value_c is None:
            last_value_c = torch.zeros(1, device=self._device)
        path_slice = slice(self.path_start_idx_list[idx], self.ptr_list[idx])
        last_value_r = last_value_r.to(self._device)
        last_value_c = last_value_c.to(self._device)
        rewards = torch.cat([self.buffers[idx]['reward'][path_slice], last_value_r])
        costs = torch.cat([self.buffers[idx]['cost'][path_slice], last_value_c])
        values_r = torch.cat([self.buffers[idx]['value_r'][path_slice], last_value_r])
        values_c = torch.cat([self.buffers[idx]['value_c'][path_slice], last_value_c])

        adv_r, target_value_r = calculate_adv_and_value_targets(
            values_r,
            rewards,
            lam=self._lam,
            gamma=self._gamma,
        )
        adv_c, target_value_c = calculate_adv_and_value_targets(
            values_c,
            costs,
            lam=self._lam_c,
            gamma=self._gamma,
        )
        self.buffers[idx]['adv_r'][path_slice] = adv_r
        self.buffers[idx]['adv_c'][path_slice] = adv_c
        self.buffers[idx]['target_value_r'][path_slice] = target_value_r
        self.buffers[idx]['target_value_c'][path_slice] = target_value_c
        
        self.path_start_idx_list[idx] = self.ptr_list[idx]

    def get(self) -> dict[str, torch.Tensor]:
        data_pre = {k: [v] for k, v in self.buffers[0].items()}
        for buffer in self.buffers[1:]:
            for k, v in buffer.items():
                data_pre[k].append(v)
        data = {k: torch.cat(v, dim=0) for k, v in data_pre.items()}
        adv_mean = data['adv_r'].mean()
        adv_std = data['adv_r'].std()
        cadv_mean = data['adv_c'].mean()
        if self._standardized_adv_r:
            data['adv_r'] = (data['adv_r'] - adv_mean) / (adv_std + 1e-8)
        if self._standardized_adv_c:
            data['adv_c'] = data['adv_c'] - cadv_mean
        self.ptr_list = [0] * self.num_envs
        self.path_start_idx_list = [0] * self.num_envs

        return data
    
def get_flat_params_from(model: torch.nn.Module) -> torch.Tensor:
    flat_params = []
    for _, param in model.named_parameters():
        if param.requires_grad:
            data = param.data
            data = data.view(-1)  # flatten tensor
            flat_params.append(data)
    assert flat_params, 'No gradients were found in model parameters.'
    return torch.cat(flat_params)

def conjugate_gradients(
    fisher_product: Callable[[torch.Tensor], torch.Tensor],
    policy: Policy,
    fvp_obs: torch.Tensor,
    vector_b: torch.Tensor,
    num_steps: int = 10,
    residual_tol: float = 1e-10,
    eps: float = 1e-6,
) -> torch.Tensor:
    vector_x = torch.zeros_like(vector_b)
    vector_r = vector_b - fisher_product(vector_x, policy, fvp_obs)
    vector_p = vector_r.clone()
    rdotr = torch.dot(vector_r, vector_r)

    for _ in range(num_steps):
        vector_z = fisher_product(vector_p, policy, fvp_obs)
        alpha = rdotr / (torch.dot(vector_p, vector_z) + eps)
        vector_x += alpha * vector_p
        vector_r -= alpha * vector_z
        new_rdotr = torch.dot(vector_r, vector_r)
        if torch.sqrt(new_rdotr) < residual_tol:
            break
        vector_mu = new_rdotr / (rdotr + eps)
        vector_p = vector_r + vector_mu * vector_p
        rdotr = new_rdotr
    return vector_x

def set_param_values_to_model(model: torch.nn.Module, vals: torch.Tensor) -> None:
    assert isinstance(vals, torch.Tensor)
    i: int = 0
    for _, param in model.named_parameters():
        if param.requires_grad:  # param has grad and, hence, must be set
            orig_size = param.size()
            size = np.prod(list(param.size()))
            new_values = vals[i : int(i + size)]
            # set new param values
            new_values = new_values.view(orig_size)
            param.data = new_values
            i += int(size)  # increment array position
    assert i == len(vals), f'Lengths do not match: {i} vs. {len(vals)}'

def get_flat_gradients_from(model: torch.nn.Module) -> torch.Tensor:
    grads = []
    for _, param in model.named_parameters():
        if param.requires_grad and param.grad is not None:
            grad = param.grad
            grads.append(grad.view(-1))  # flatten tensor and append
    assert grads, 'No gradients were found in model parameters.'
    return torch.cat(grads)

def fvp(
    params: torch.Tensor,
    policy: Policy,
    fvp_obs: torch.Tensor,
    ) -> torch.Tensor:
    policy.actor.zero_grad()
    q_dist = policy.actor(fvp_obs)
    with torch.no_grad():
        p_dist = policy.actor(fvp_obs)
    kl = torch.distributions.kl.kl_divergence(p_dist, q_dist).mean()

    grads = torch.autograd.grad(
        kl,
        tuple(policy.actor.parameters()),
        create_graph=True,
    )
    flat_grad_kl = torch.cat([grad.view(-1) for grad in grads])

    kl_p = (flat_grad_kl * params).sum()
    grads = torch.autograd.grad(
        kl_p,
        tuple(policy.actor.parameters()),
        retain_graph=False,
    )

    flat_grad_grad_kl = torch.cat([grad.contiguous().view(-1) for grad in grads])

    logger.store(
        **{
            'Train/KL': kl.item(),
        },
    )
    return flat_grad_grad_kl + params * args.cg_damping


if __name__ == "__main__":
    args = parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.set_num_threads(2)

    device = torch.device("cuda" if torch.cuda.is_available() and args.device == "cuda" else "cpu")

    local_steps_per_epoch = args.steps_per_epoch//args.num_envs
    epochs = args.total_steps // args.steps_per_epoch

    if args.num_envs > 1:
        env = safety_gymnasium.vector.make(env_id=args.env_id, num_envs=args.num_envs, wrappers=SafeNormalizeObservation)
        obs_space = env.single_observation_space
        act_space = env.single_action_space
        env = SafeNormalizeObservation(env)
    else:
        env = safety_gymnasium.make(args.env_id)
        obs_space = env.observation_space
        act_space = env.action_space
        env = SafeAutoResetWrapper(env)
        env = SafeRescaleAction(env, -1.0, 1.0)
        env = SafeNormalizeObservation(env)
        env = SafeUnsqueeze(env)
    policy = Policy(
        obs_dim=obs_space.shape[0],
        act_dim=act_space.shape[0],
    ).to(device)
    reward_critic_optimizer = torch.optim.Adam(policy.reward_critic.parameters(), lr=args.critic_lr)

    buffer = VectorizedBuffer(
        obs_space=obs_space,
        act_space=act_space,
        size = args.steps_per_epoch,
        gamma = args.gamma,
        lam = args.lam,
        lam_c = args.lam_c,
        standardized_adv_r=args.standardized_adv_r,
        standardized_adv_c=args.standardized_adv_c,
        device=device,
        num_envs = args.num_envs,
    )

    dict_args = vars(args)
    exp_name = "-".join([args.env_id, "ppo", "seed-" + str(args.seed)])
    logger = EpochLogger(
        base_dir=args.log_dir,
        seed=str(args.seed),
        exp_name=exp_name,
        use_tensorboard=args.use_tensorboard,
    )
    rew_deque = deque(maxlen=50)
    cost_deque = deque(maxlen=50)
    len_deque = deque(maxlen=50)
    logger.save_config(dict_args)
    logger.setup_torch_saver(policy.actor)
    logger.log("Start with training.")

    start_time = time.time()

    for epoch in range(epochs):
        rollout__start_time = time.time()    
        obs, _ = env.reset()
        obs = torch.as_tensor(obs, dtype=torch.float32, device=device)
        ep_ret, ep_cost, ep_len = np.zeros(args.num_envs), np.zeros(args.num_envs), np.zeros(args.num_envs)
        for steps in range(local_steps_per_epoch):
            with torch.no_grad():
                act, log_prob, value_r, value_c = policy.step(obs, deterministic=False)
            next_obs, reward, cost, terminated, truncated, info = env.step(act.detach().squeeze().cpu().numpy())
            ep_ret += reward
            ep_cost += cost
            ep_len += 1
            next_obs, reward, cost, terminated, truncated = (
                torch.as_tensor(x, dtype=torch.float32, device=device) for x in (next_obs, reward, cost, terminated, truncated)
            )
            if 'final_observation' in info:
                info['final_observation'] = np.array(
                    [
                        array if array is not None else np.zeros(obs.shape[-1])
                        for array in info['final_observation']
                    ],
                )
                info['final_observation'] = torch.as_tensor(
                    info['final_observation'],
                    dtype=torch.float32,
                    device=device,
                )
            buffer.store(
                obs=obs,
                act=act,
                reward=reward,
                cost=cost,
                value_r=value_r,
                value_c=value_c,
                log_prob=log_prob,
            )

            obs = next_obs
            epoch_end = steps >= local_steps_per_epoch - 1
            for idx, (done, time_out) in enumerate(zip(terminated, truncated)):
                if epoch_end or done or time_out:
                    last_value_r = torch.zeros(1, device=device)
                    last_value_c = torch.zeros(1, device=device)
                    if not done:
                        if epoch_end:
                            with torch.no_grad():
                                _, _, last_value_r, last_value_c = policy.step(obs[idx], deterministic=False)
                        if time_out:
                            with torch.no_grad():
                                _, _, last_value_r, last_value_c = policy.step(
                                    info['final_observation'][idx],
                                    deterministic=False
                                )
                        last_value_r = last_value_r.unsqueeze(0)
                        last_value_c = last_value_c.unsqueeze(0)
                    if done or time_out:
                        rew_deque.append(ep_ret[idx])
                        cost_deque.append(ep_cost[idx])
                        len_deque.append(ep_len[idx])
                        logger.store(
                        **{
                            "Metrics/EpRet": np.mean(rew_deque), 
                            "Metrics/EpCosts": np.mean(cost_deque),
                            "Metrics/EpLen": np.mean(len_deque), 
                          }
                        )
                        ep_ret[idx] = 0.0
                        ep_cost[idx] = 0.0
                        ep_len[idx] = 0.0

                    buffer.finish_path(last_value_r = last_value_r, last_value_c=last_value_c, idx = idx)
        rollout_end_time = time.time()
        # update
        data = buffer.get()
        fvp_obs = data['obs'][:: args.fvp_sample_freq]
        theta_old = get_flat_params_from(policy.actor)
        policy.actor.zero_grad()

        # compute loss_pi
        distribution = policy.actor(data['obs'])
        log_prob = distribution.log_prob(data['act']).sum(dim=-1)
        ratio = torch.exp(log_prob - data['log_prob'])
        loss_pi = -(ratio * data['adv_r']).mean()

        loss_pi.backward()

        grads = -get_flat_gradients_from(policy.actor)
        x = conjugate_gradients(fvp, policy, fvp_obs, grads, args.cg_iters)
        assert torch.isfinite(x).all(), 'x is not finite'
        xHx = torch.dot(x, fvp(x, policy, fvp_obs,))
        assert xHx.item() >= 0, 'xHx is negative'
        alpha = torch.sqrt(2 * args.target_kl / (xHx + 1e-8))
        step_direction = x * alpha
        assert torch.isfinite(step_direction).all(), 'step_direction is not finite'

        theta_new = theta_old + step_direction
        set_param_values_to_model(policy.actor, theta_new)

        logger.store(
            **{
                'Misc/Alpha': alpha.item(),
                'Misc/FinalStepNorm': torch.norm(step_direction).mean().item(),
                'Misc/xHx': xHx.item(),
                'Misc/gradient_norm': torch.norm(grads).mean().item(),
                'Misc/H_inv_g': x.norm().item(),
                "Loss/Loss_actor": loss_pi.mean().item(),
            },
        )

        dataloader = DataLoader(
            dataset=TensorDataset(
                data['obs'], 
                data['target_value_r'], 
                ),
            batch_size=args.batch_size,
            shuffle=True,
        )
        for _ in track(range(args.update_iters), description='Updating...'):
            for (
                obs_b,
                target_value_r_b,
            ) in dataloader:
                reward_critic_optimizer.zero_grad()
                loss_r = nn.functional.mse_loss(policy.reward_critic(obs_b), target_value_r_b)
                for param in policy.reward_critic.parameters():
                    loss_r += param.pow(2).sum() * args.critic_norm_coef
                loss_r.backward()
                reward_critic_optimizer.step()

                logger.store(**{"Loss/Loss_reward_critic": loss_r.mean().item(),})
        update_end_time = time.time()
        # log data
        logger.log_tabular("Metrics/EpRet", min_and_max=True, std=True)
        logger.log_tabular("Metrics/EpCosts", min_and_max=True, std=True)
        logger.log_tabular("Metrics/EpLen", min_and_max=True)
        logger.log_tabular('Train/Epoch', epoch+1)
        logger.log_tabular('Train/TotalSteps', (epoch+1)*args.steps_per_epoch)
        logger.log_tabular('Train/KL')
        logger.log_tabular("Loss/Loss_reward_critic")
        logger.log_tabular("Loss/Loss_actor")
        logger.log_tabular('Time/Rollout', rollout_end_time - rollout__start_time)
        logger.log_tabular('Time/Update', update_end_time - rollout_end_time)
        logger.log_tabular('Value/RewardAdv', data['adv_r'].mean().item())
        logger.log_tabular('Value/CostAdv', data['adv_c'].mean().item())
        logger.log_tabular('Misc/Alpha')
        logger.log_tabular('Misc/FinalStepNorm')
        logger.log_tabular('Misc/xHx')
        logger.log_tabular('Misc/gradient_norm')
        logger.log_tabular('Misc/H_inv_g')

        logger.dump_tabular()
        if epoch % 100 == 0:
            logger.torch_save(itr=epoch)
    logger.close()
