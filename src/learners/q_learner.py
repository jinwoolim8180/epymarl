import copy

import torch as th
from torch.optim import Adam, SGD

from components.episode_buffer import EpisodeBatch
from components.standarize_stream import RunningMeanStd
from modules.mixers.vdn import VDNMixer
from modules.mixers.qmix import QMixer


class QLearner:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.n_agents = args.n_agents
        self.mac = mac
        self.logger = logger

        if self.args.fl or self.args.pfl or self.args.regularization:
            for i in range(1, self.n_agents):
                self.mac.agent.agents[i].load_state_dict(self.mac.agent.agents[0].state_dict())

        self.round_steps = 0
        self.w = copy.deepcopy(mac)
        self.g = copy.deepcopy(mac)
        self.weight = th.nn.Parameter(th.eye(self.n_agents))

        self.mac.cuda()
        self.w.cuda()
        self.g.cuda()

        if self.args.pfl:
            self.w_avg = copy.deepcopy(self.g.agent.agents[0])
            self.w_avg.cuda()

        self.params = list(mac.parameters())
        self.w_params = list(self.w.parameters())
        self.last_target_update_episode = 0

        self.mixer = None
        if args.mixer is not None:
            if args.mixer == "vdn":
                assert args.common_reward, "VDN only supports common reward setting"
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                assert args.common_reward, "QMIX only supports common reward setting"
                self.mixer = QMixer(args)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())
            self.target_mixer = copy.deepcopy(self.mixer)

        self.optimiser = Adam(params=[{'params': self.params}, {'params': self.weight}], lr=args.lr)
        self.w_optimiser = Adam(params=self.w_params, lr=args.lr * self.args.local_step)

        # a little wasteful to deepcopy (e.g. duplicates action selector), but should work for any MAC
        self.target_mac = copy.deepcopy(mac)

        self.training_steps = 0
        self.last_target_update_step = 0
        self.log_stats_t = -self.args.learner_log_interval - 1

        device = "cuda" if args.use_cuda else "cpu"
        self.weight = self.weight.to(device)
        if self.args.standardise_returns:
            self.ret_ms = RunningMeanStd(shape=(self.n_agents,), device=device)
        if self.args.standardise_rewards:
            rew_shape = (1,) if self.args.common_reward else (self.n_agents,)
            self.rew_ms = RunningMeanStd(shape=rew_shape, device=device)

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities
        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :-1]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"]

        if self.args.standardise_rewards:
            self.rew_ms.update(rewards)
            rewards = (rewards - self.rew_ms.mean) / th.sqrt(self.rew_ms.var)

        if self.args.common_reward:
            assert (
                rewards.size(2) == 1
            ), "Expected singular agent dimension for common rewards"
            # reshape rewards to be of shape (batch_size, episode_length, n_agents)
            rewards = rewards.expand(-1, -1, self.n_agents)

        # Calculate estimated Q-Values
        mac_out = []
        self.mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            agent_outs = self.mac.forward(batch, t=t)
            mac_out.append(agent_outs)
        mac_out = th.stack(mac_out, dim=1)  # Concat over time
        # Pick the Q-Values for the actions taken by each agent
        chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(
            3
        )  # Remove the last dim

        # Calculate the Q-Values necessary for the target
        target_mac_out = []
        self.target_mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            target_agent_outs = self.target_mac.forward(batch, t=t)
            target_mac_out.append(target_agent_outs)

        # We don't need the first timesteps Q-Value estimate for calculating targets
        target_mac_out = th.stack(target_mac_out[1:], dim=1)  # Concat across time

        # Mask out unavailable actions
        target_mac_out[avail_actions[:, 1:] == 0] = -9999999

        # Max over target Q-Values
        if self.args.double_q:
            # Get actions that maximise live Q (for double q-learning)
            mac_out_detach = mac_out.clone().detach()
            mac_out_detach[avail_actions == 0] = -9999999
            cur_max_actions = mac_out_detach[:, 1:].max(dim=3, keepdim=True)[1]
            target_max_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]

        # Mix
        if self.mixer is not None:
            chosen_action_qvals = self.mixer(
                chosen_action_qvals, batch["state"][:, :-1]
            )
            target_max_qvals = self.target_mixer(
                target_max_qvals, batch["state"][:, 1:]
            )

        if self.args.standardise_returns:
            target_max_qvals = (
                target_max_qvals * th.sqrt(self.ret_ms.var) + self.ret_ms.mean
            )

        # Calculate 1-step Q-Learning targets
        targets = (
            rewards + self.args.gamma * (1 - terminated) * target_max_qvals.detach()
        )

        if self.args.standardise_returns:
            self.ret_ms.update(targets)
            targets = (targets - self.ret_ms.mean) / th.sqrt(self.ret_ms.var)

        # Td-error
        td_error = chosen_action_qvals - targets.detach()

        mask = mask.expand_as(td_error)

        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask

        # Normal L2 loss, take mean over actual data
        loss = (masked_td_error**2).sum() / mask.sum()

        if self.args.pfl:
            w_loss = 0
            for param, w_param in zip(self.mac.parameters(), self.w.parameters()):
                w_loss += (param - w_param).norm(2) ** 2
            loss += 0.5 * self.args.pfl_lambda * w_loss

        if self.args.regularization:
            w_loss = 0
            # W = th.softmax(self.weight, dim=-1)
            for i in range(self.n_agents):
                for j in range(self.n_agents):
                    for param, w_param in zip(self.mac.agent.agents[i].parameters(),
                                              self.mac.agent.agents[j].parameters()):
                        w_loss += self.weight[i, j] * (param - w_param).norm(2) ** 2
            loss += 0.5 * self.args.pfl_lambda * w_loss

        # Optimise
        self.optimiser.zero_grad()
        loss.backward()
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)

        # if self.args.fl:
        #     for i in range(self.n_agents):
        #         for param, g_param, w_avg_param in zip(self.mac.agent.agents[i].parameters(),
        #                                                self.g.agent.agents[i].parameters(),
        #                                                self.w_avg.parameters()):
        #             param.grad -= g_param.data
        #             param.grad += w_avg_param.data

        self.optimiser.step()

        self.training_steps += 1

        if self.args.fl:
            if self.training_steps > self.args.t_max // 2 and self.training_steps % self.args.local_step == 0:
                # for param, w_param, g_param, w_avg_param in zip(self.mac.agent.agents[i].parameters(),
                #                                 self.w.agent.agents[i].parameters(),
                #                                 self.g.agent.agents[i].parameters(),
                #                                 self.w_avg.parameters()):
                #     g_param.data = g_param.data - w_avg_param.data + (w_param.data - param.data) / (self.args.lr * self.args.local_step)
                #     w_param.data = param.data
                # # for i in range(self.n_agents):
                # #     self.w.agent.agents[i].load_state_dict(self.mac.agent.agents[i].state_dict())

                # # self.w_avg = copy.deepcopy(self.w.agent.agents[0])
                # # self.w_avg.cuda()
                # for k in self.w_avg.state_dict().keys():
                #     self.w_avg.state_dict()[k] *= 0
                #     for i in range(self.n_agents):
                #         self.w_avg.state_dict()[k] += self.g.agent.agents[i].state_dict()[k]
                #     self.w_avg.state_dict()[k] /= self.n_agents
                
                # MAC FedAvg
                mac_avg = copy.deepcopy(self.mac.agent.agents[0].state_dict())
                for k in mac_avg.keys():
                    for i in range(1, self.n_agents):
                        mac_avg[k] += self.mac.agent.agents[i].state_dict()[k]
                    mac_avg[k] = th.div(mac_avg[k], self.n_agents)
                for i in range(0, self.n_agents):
                    for k in mac_avg.keys():
                        r = 1.0
                        self.mac.agent.agents[i].state_dict()[k] *= 1 - r
                        self.mac.agent.agents[i].state_dict()[k] += r * mac_avg[k]

                # Step 2: Average optimizer states for Adam
                # param_names = [name for name, _ in self.mac.agent.agents[0].named_parameters()]
                # for name in param_names:
                #     exp_avgs = []
                #     exp_avg_sqs = []
                #     # Collect states for this parameter across all agents
                #     for i in range(self.n_agents):
                #         param = dict(self.mac.agent.agents[i].named_parameters())[name]
                #         if param in self.optimiser.state:
                #             state = self.optimiser.state[param]
                #             exp_avgs.append(state['exp_avg'])
                #             exp_avg_sqs.append(state['exp_avg_sq'])
                #     if exp_avgs:  # Only proceed if states exist
                #         # Compute averages
                #         avg_exp_avg = sum(exp_avgs) / self.n_agents
                #         avg_exp_avg_sq = sum(exp_avg_sqs) / self.n_agents
                #         # Assign averaged states back to each agent's parameter
                #         for i in range(self.n_agents):
                #             param = dict(self.mac.agent.agents[i].named_parameters())[name]
                #             self.optimiser.state[param]['exp_avg'] = avg_exp_avg.clone()
                #             self.optimiser.state[param]['exp_avg_sq'] = avg_exp_avg_sq.clone()

            # Target MAC FedAvg
            # target_mac_avg = copy.deepcopy(self.target_mac.agent.agents[0].state_dict())
            # for k in target_mac_avg.keys():
            #     for i in range(1, self.n_agents):
            #         target_mac_avg[k] += self.target_mac.agent.agents[i].state_dict()[k]
            #     target_mac_avg[k] = th.div(target_mac_avg[k], self.n_agents)
            # for i in range(0, self.n_agents):
            #     self.target_mac.agent.agents[i].load_state_dict(target_mac_avg)

        if self.args.pfl:
            if self.training_steps % self.args.local_step == 0:
                if self.round_steps == 0:
                    for i in range(self.n_agents):
                        self.w.agent.agents[i].load_state_dict(self.w_avg.state_dict())

                self.w_optimiser.zero_grad()
                w_loss = 0
                for param, w_param in zip(self.mac.parameters(), self.w.parameters()):
                    w_loss += 0.5 * (param - w_param).norm(2) ** 2
                w_loss.backward()        
                self.w_optimiser.step()
                self.round_steps += 1

                if self.round_steps == self.args.local_round:
                    # MAC FedAvg
                    self.w_avg = copy.deepcopy(self.w.agent.agents[0])
                    for k in self.w_avg.state_dict().keys():
                        for i in range(1, self.n_agents):
                            self.w_avg.state_dict()[k] += self.w.agent.agents[i].state_dict()[k]
                        self.w_avg.state_dict()[k] /= self.n_agents
                    for i in range(self.n_agents):
                        self.mac.agent.agents[i].load_state_dict(self.w.agent.agents[i].state_dict())
                    self.round_steps = 0
                    

        if (
            self.args.target_update_interval_or_tau > 1
            and (self.training_steps - self.last_target_update_step)
            / self.args.target_update_interval_or_tau
            >= 1.0
        ):
            self._update_targets_hard()
            self.last_target_update_step = self.training_steps
        elif self.args.target_update_interval_or_tau <= 1.0:
            self._update_targets_soft(self.args.target_update_interval_or_tau)


        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            self.logger.log_stat("loss", loss.item(), t_env)
            self.logger.log_stat("grad_norm", grad_norm.item(), t_env)
            mask_elems = mask.sum().item()
            self.logger.log_stat(
                "td_error_abs", (masked_td_error.abs().sum().item() / mask_elems), t_env
            )
            self.logger.log_stat(
                "q_taken_mean",
                (chosen_action_qvals * mask).sum().item()
                / (mask_elems * self.args.n_agents),
                t_env,
            )
            self.logger.log_stat(
                "target_mean",
                (targets * mask).sum().item() / (mask_elems * self.args.n_agents),
                t_env,
            )
            self.log_stats_t = t_env

    def _update_targets_hard(self):
        self.target_mac.load_state(self.mac)
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())

    def _update_targets_soft(self, tau):
        for target_param, param in zip(
            self.target_mac.parameters(), self.mac.parameters()
        ):
            target_param.data.copy_(target_param.data * (1.0 - tau) + param.data * tau)
        if self.mixer is not None:
            for target_param, param in zip(
                self.target_mixer.parameters(), self.mixer.parameters()
            ):
                target_param.data.copy_(
                    target_param.data * (1.0 - tau) + param.data * tau
                )

    def cuda(self):
        self.mac.cuda()
        self.target_mac.cuda()
        if self.mixer is not None:
            self.mixer.cuda()
            self.target_mixer.cuda()

    def save_models(self, path):
        self.mac.save_models(path)
        if self.mixer is not None:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path):
        self.mac.load_models(path)
        # Not quite right but I don't want to save target networks
        self.target_mac.load_models(path)
        if self.mixer is not None:
            self.mixer.load_state_dict(
                th.load(
                    "{}/mixer.th".format(path),
                    map_location=lambda storage, loc: storage,
                )
            )
        self.optimiser.load_state_dict(
            th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage)
        )
