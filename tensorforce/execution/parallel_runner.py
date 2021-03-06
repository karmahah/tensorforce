# Copyright 2018 Tensorforce Team. All Rights Reserved.
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

import time
from tqdm import tqdm

import numpy as np

from tensorforce import Agent, Environment, TensorforceError, util


class ParallelRunner(object):
    """
    Tensorforce parallel runner utility.

    Args:
        agent (specification | Agent object): Agent specification or object, the latter is not
            closed automatically as part of `runner.close()`
            (<span style="color:#C00000"><b>required</b></span>).
        environment (specification | Environment object): Environment specification or object, the
            latter is not closed automatically as part of `runner.close()`
            (<span style="color:#C00000"><b>required</b></span>, or alternatively `environments`).
        num_parallel (int > 0): Number of parallel environment instances to run
            (<span style="color:#C00000"><b>required</b></span>, or alternatively `environments`).
        environments (list[specification | Environment object]): Environment specifications or
            objects, the latter are not closed automatically as part of `runner.close()`
            (<span style="color:#C00000"><b>required</b></span>, or alternatively `environment` and
            `num_parallel`).
        max_episode_timesteps (int > 0): Maximum number of timesteps per episode, overwrites the
            environment default if defined
            (<span style="color:#00C000"><b>default</b></span>: environment default).
        evaluation_environment (specification | Environment object): Evaluation environment or
            object, the latter is not closed automatically as part of `runner.close()`
            (<span style="color:#00C000"><b>default</b></span>: none).
        save_best_agent (string): Directory to save the best version of the agent according to the
            evaluation
            (<span style="color:#00C000"><b>default</b></span>: best agent is not saved).
    """

    def __init__(
        self, agent, environment=None, num_parallel=None, environments=None,
        max_episode_timesteps=None, evaluation_environment=None, save_best_agent=None
    ):
        self.environments = list()
        if environment is None:
            assert num_parallel is None and environments is not None
            if not util.is_iterable(x=environments):
                raise TensorforceError.type(
                    name='parallel-runner', argument='environments', value=environments
                )
            elif len(environments) == 0:
                raise TensorforceError.value(
                    name='parallel-runner', argument='environments', value=environments
                )
            num_parallel = len(environments)
            environment = environments[0]
            self.is_environment_external = isinstance(environment, Environment)
            environment = Environment.create(
                environment=environment, max_episode_timesteps=max_episode_timesteps
            )
            states = environment.states()
            actions = environment.actions()
            self.environments.append(environment)
            for environment in environments[1:]:
                assert isinstance(environment, Environment) == self.is_environment_external
                environment = Environment.create(
                    environment=environment, max_episode_timesteps=max_episode_timesteps
                )
                assert environment.states() == states
                assert environment.actions() == actions
                self.environments.append(environment)

        else:
            assert num_parallel is not None and environments is None
            assert not isinstance(environment, Environment)
            self.is_environment_external = False
            for _ in range(num_parallel):
                environment = Environment.create(
                    environment=environment, max_episode_timesteps=max_episode_timesteps
                )
                self.environments(environment)

        if evaluation_environment is None:
            self.evaluation_environment = None
        else:
            self.is_eval_environment_external = isinstance(evaluation_environment, Environment)
            self.evaluation_environment = Environment.create(
                environment=evaluation_environment, max_episode_timesteps=max_episode_timesteps
            )
            assert self.evaluation_environment.states() == environment.states()
            assert self.evaluation_environment.actions() == environment.actions()

        self.is_agent_external = isinstance(agent, Agent)
        kwargs = dict(parallel_interactions=num_parallel)
        self.agent = Agent.create(agent=agent, environment=environment, **kwargs)
        self.save_best_agent = save_best_agent

        self.episode_rewards = list()
        self.episode_timesteps = list()
        self.episode_seconds = list()
        self.episode_agent_seconds = list()
        self.evaluation_rewards = list()
        self.evaluation_timesteps = list()
        self.evaluation_seconds = list()
        self.evaluation_agent_seconds = list()

    def close(self):
        if hasattr(self, 'tqdm'):
            self.tqdm.close()
        if not self.is_agent_external:
            self.agent.close()
        if not self.is_environment_external:
            for environment in self.environments:
                environment.close()
        if self.evaluation_environment is not None and not self.is_eval_environment_external:
            self.evaluation_environment.close()
        self.agent.close()

    # TODO: make average reward another possible criteria for runner-termination
    def run(
        self,
        # General
        num_episodes=None, num_timesteps=None, num_updates=None, join_agent_calls=False,
        sync_timesteps=False, sync_episodes=False, num_sleep_secs=0.01,
        # Callback
        callback=None, callback_episode_frequency=None, callback_timestep_frequency=None,
        # Tqdm
        use_tqdm=True, mean_horizon=1,
        # Evaluation
        evaluation_callback=None,
    ):
        # General
        if num_episodes is None:
            self.num_episodes = float('inf')
        else:
            self.num_episodes = num_episodes
        if num_timesteps is None:
            self.num_timesteps = float('inf')
        else:
            self.num_timesteps = num_timesteps
        if num_updates is None:
            self.num_updates = float('inf')
        else:
            self.num_updates = num_updates
        self.join_agent_calls = join_agent_calls
        if self.join_agent_calls:
            sync_timesteps = True
        self.sync_timesteps = sync_timesteps
        self.sync_episodes = sync_episodes
        self.num_sleep_secs = num_sleep_secs

        # Callback
        assert callback_episode_frequency is None or callback_timestep_frequency is None
        if callback_episode_frequency is None and callback_timestep_frequency is None:
            callback_episode_frequency = 1
        if callback_episode_frequency is None:
            self.callback_episode_frequency = float('inf')
        else:
            self.callback_episode_frequency = callback_episode_frequency
        if callback_timestep_frequency is None:
            self.callback_timestep_frequency = float('inf')
        else:
            self.callback_timestep_frequency = callback_timestep_frequency
        if callback is None:
            self.callback = (lambda r, p: True)
        elif util.is_iterable(x=callback):
            def sequential_callback(runner, parallel):
                result = True
                for fn in callback:
                    x = fn(runner, parallel)
                    if isinstance(result, bool):
                        result = result and x
                return result
            self.callback = sequential_callback
        else:
            def boolean_callback(runner, parallel):
                result = callback(runner, parallel)
                if isinstance(result, bool):
                    return result
                else:
                    return True
            self.callback = boolean_callback

        # Timestep/episode/update counter
        self.timesteps = 0
        self.episodes = 0
        self.updates = 0

        # Tqdm
        if use_tqdm:
            if hasattr(self, 'tqdm'):
                self.tqdm.close()

            assert self.num_episodes != float('inf') or self.num_timesteps != float('inf')
            inner_callback = self.callback

            if self.num_episodes != float('inf'):
                # Episode-based tqdm (default option if both num_episodes and num_timesteps set)
                assert self.num_episodes != float('inf')
                bar_format = (
                    '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}, reward={postfix[0]:.2f}, ts/ep='
                    '{postfix[1]}, sec/ep={postfix[2]:.2f}, ms/ts={postfix[3]:.1f}, agent='
                    '{postfix[4]:.1f}%]'
                )
                postfix = [0.0, 0, 0.0, 0.0, 0.0]
                self.tqdm = tqdm(
                    desc='Episodes', total=self.num_episodes, bar_format=bar_format,
                    initial=self.episodes, postfix=postfix
                )
                self.tqdm_last_update = self.episodes

                def tqdm_callback(runner, parallel):
                    mean_reward = float(np.mean(runner.episode_rewards[-mean_horizon:]))
                    mean_ts_per_ep = int(np.mean(runner.episode_timesteps[-mean_horizon:]))
                    mean_sec_per_ep = float(np.mean(runner.episode_seconds[-mean_horizon:]))
                    mean_agent_sec = float(np.mean(runner.episode_agent_seconds[-mean_horizon:]))
                    mean_ms_per_ts = mean_sec_per_ep * 1000.0 / mean_ts_per_ep
                    mean_rel_agent = mean_agent_sec * 100.0 / mean_sec_per_ep
                    runner.tqdm.postfix[0] = mean_reward
                    runner.tqdm.postfix[1] = mean_ts_per_ep
                    runner.tqdm.postfix[2] = mean_sec_per_ep
                    runner.tqdm.postfix[3] = mean_ms_per_ts
                    runner.tqdm.postfix[4] = mean_rel_agent
                    runner.tqdm.update(n=(runner.episodes - runner.tqdm_last_update))
                    runner.tqdm_last_update = runner.episodes
                    return inner_callback(runner, parallel)

            else:
                # Timestep-based tqdm
                self.tqdm = tqdm(
                    desc='Timesteps', total=self.num_timesteps, initial=self.timesteps,
                    postfix=dict(mean_reward='n/a')
                )
                self.tqdm_last_update = self.timesteps

                def tqdm_callback(runner, parallel):
                    # sum_timesteps_reward = sum(runner.timestep_rewards[num_mean_reward:])
                    # num_timesteps = min(num_mean_reward, runner.episode_timestep)
                    # mean_reward = sum_timesteps_reward / num_episodes
                    runner.tqdm.set_postfix(mean_reward='n/a')
                    runner.tqdm.update(n=(runner.timesteps - runner.tqdm_last_update))
                    runner.tqdm_last_update = runner.timesteps
                    return inner_callback(runner, parallel)

            self.callback = tqdm_callback

        # Evaluation
        if self.evaluation_environment is None:
            assert evaluation_callback is None
            assert self.save_best_agent is None
        else:
            if evaluation_callback is None:
                self.evaluation_callback = (lambda r: None)
            else:
                self.evaluation_callback = evaluation_callback
            if self.save_best_agent is not None:
                inner_evaluation_callback = self.evaluation_callback

                def mean_reward_callback(runner):
                    result = inner_evaluation_callback(runner)
                    if result is None:
                        return runner.evaluation_reward
                    else:
                        return result

                self.evaluation_callback = mean_reward_callback
                self.best_evaluation_score = None

        # Required if agent was previously stopped mid-episode
        self.agent.reset()

        # Reset environments and episode statistics
        for environment in self.environments:
            environment.start_reset()
        self.episode_reward = [0.0 for _ in self.environments]
        self.episode_timestep = [0 for _ in self.environments]
        if self.join_agent_calls:
            self.episode_agent_second = 0.0
            self.episode_start = time.time()
        else:
            self.episode_agent_second = [0.0 for _ in self.environments]
            self.episode_start = [time.time() for _ in self.environments]
        environments = list(self.environments)

        if self.evaluation_environment is not None:
            self.evaluation_environment.start_reset()
            self.evaluation_reward = 0.0
            self.evaluation_timestep = 0
            if not self.join_agent_calls:
                self.evaluation_agent_second = 0.0
            environments.append(self.evaluation_environment)

        self.finished = False
        self.prev_terminals = [0 for _ in environments]
        self.states = [None for _ in environments]
        self.terminals = [None for _ in environments]
        self.rewards = [None for _ in environments]

        if self.join_agent_calls:
            self.joint


        # Runner loop
        while not self.finished:

            if self.join_agent_calls:
                # Retrieve observations (only if not already terminated)
                self.observations = [None for _ in environments]
                while any(observation is None for observation in self.observations):
                    for n, (environment, terminal) in enumerate(zip(environments, self.prev_terminals)):
                        if self.observations[n] is not None:
                            continue
                        if terminal == 0:
                            self.observations[n] = environment.receive_execute()
                        else:
                            self.observations[n] = (None, terminal, None)
                self.states, self.terminals, self.rewards = zip(self.observations)
                self.terminals[parallel] = [
                    terminal if terminal is None else int(terminal) for terminal in terminals
                ]

                self.handle_observe_joint()
                self.handle_act_joint()
                # if not self.join_agent_calls:  # !!!!!!
                #     self.episode_seconds.append(time.time() - episode_start[parallel])
                #     self.episode_agent_seconds.append(self.episode_agent_second[parallel])

            else:
                self.terminals = list(self.prev_terminals)

            if not self.sync_timesteps:
                no_environment_ready = True

            # Parallel environments loop
            for parallel, environment in enumerate(environments):

                # Is evaluation environment?
                evaluation = (parallel == len(self.environments))

                if self.sync_episodes and self.prev_terminals[parallel] > 0:
                    # Continue if episode already terminated
                    continue

                elif self.join_agent_calls:
                    pass

                elif self.sync_timesteps:
                    # Wait until environment is ready
                    while True:
                        observation = environment.receive_execute()
                        if observation is not None:
                            break

                else:
                    # Check whether environment is ready, otherwise continue
                    observation = environment.receive_execute()
                    if observation is None:
                        continue
                    no_environment_ready = False

                if not self.join_agent_calls:
                    self.states[parallel], self.terminals[parallel], self.rewards[parallel] = observation
                    if self.terminals[parallel] is not None:
                        self.terminals[parallel] = int(self.terminals[parallel])

                if self.terminals[parallel] is None:
                    # Initial act
                    if evaluation:
                        self.handle_act_evaluation()
                    else:
                        self.handle_act(parallel=parallel)

                else:
                    # Observe
                    if evaluation:
                        self.handle_observe_evaluation()
                    else:
                        self.handle_observe(parallel=parallel)

                    if self.terminals[parallel] == 0:
                        # Act
                        if evaluation:
                            self.handle_act_evaluation()
                        else:
                            self.handle_act(parallel=parallel)

                    else:
                        # Terminal
                        if evaluation:
                            self.handle_terminal_evaluation()
                        else:
                            self.handle_terminal(parallel=parallel)

                # # Update global timesteps/episodes/updates
                # self.global_timesteps = self.agent.timesteps
                # self.global_episodes = self.agent.episodes
                # self.global_updates = self.agent.updates

            print(self.sync_episodes)
            if self.sync_episodes and all(terminal > 0 for terminal in self.terminals):
                # Reset if all episodes terminated
                self.prev_terminals = [0 for _ in environments]
                for environment in environments:
                    environment.start_reset()
            else:
                self.prev_terminals = list(self.terminals)

            if not self.sync_timesteps and no_environment_ready:
                # Sleep if no environment was ready
                time.sleep(self.num_sleep_secs)

    def handle_act(self, parallel):
        print(parallel, 'act')

        if self.join_agent_calls:
            self.environments[parallel].start_execute(actions=self.actions[parallel])

        else:
            agent_start = time.time()
            actions = self.agent.act(states=self.states[parallel], parallel=parallel)
            self.episode_agent_second[parallel] += time.time() - agent_start

            self.environments[parallel].start_execute(actions=actions)

        # Increment timestep counter
        self.timesteps += 1
        self.episode_timestep[parallel] += 1

        # Maximum number of timesteps or timestep callback (after counter increment!)
        if self.timesteps >= self.num_timesteps or (
            self.episode_timestep[parallel] % self.callback_timestep_frequency == 0 and
            not self.callback(self, parallel)
        ):
            self.finished = True

    def handle_act_joint(self):
        print('act joint')

        parallel = [
            n for n, terminal in enumerate(self.terminals) if terminal is None or terminal == 0
        ]
        agent_start = time.time()
        self.actions = self.agent.act(states=[self.states[n] for n in parallel], parallel=parallel)
        self.episode_agent_second += time.time() - agent_start
        self.actions = [
            self.actions[parallel.index(n)] if n in parallel else None
            for n in range(len(environments))
        ]

    def handle_act_evaluation(self):
        print('act eval')

        if self.join_agent_calls:
            self.environments[parallel].start_execute(actions=actions[parallel])

        else:
            agent_start = time.time()
            actions = self.agent.act(states=states, evaluation=True)
            self.evaluation_agent_second += time.time() - agent_start

            self.environments[parallel].start_execute(actions=actions)

        # Update evaluation statistics
        self.evaluation_timestep += 1

    def handle_observe(self, parallel):
        print(parallel, 'observe')

        # Update episode statistics
        self.episode_reward[parallel] += self.rewards[parallel]

        # Not terminal but finished
        if self.terminals[parallel] == 0 and self.finished:
            self.terminals[parallel] = 2

        # Observe unless join_agent_calls
        if not self.join_agent_calls:
            agent_start = time.time()
            updated = self.agent.observe(
                terminal=self.terminals[parallel], reward=self.rewards[parallel],
                parallel=parallel
            )
            self.episode_agent_second[parallel] += time.time() - agent_start
            self.updates += int(updated)

        # Maximum number of updates (after counter increment!)
        if self.updates >= self.num_updates:
            self.finished = True

    def handle_observe_joint(self):
        print('observe joint')

        parallel = [
            n for n, (prev_terminal, terminal) in enumerate(zip(self.prev_terminals, self.terminals))
            if prev_terminal == 0 and terminal is not None
        ]
        agent_start = time.time()
        updated = self.agent.observe(
            terminal=[self.terminals[n] for n in parallel],
            reward=[self.rewards[n] for n in parallel], parallel=parallel
        )
        self.episode_agent_second += time.time() - agent_start
        self.updates += int(updated)

    def handle_observe_evaluation(self):
        print('observe eval')

        # Update evaluation statistics
        self.evaluation_reward += reward

        # Reset agent if terminal
        if terminal > 0:
            agent_start = time.time()
            self.agent.reset(evaluation=True)
            self.evaluation_agent_second += time.time() - agent_start

    def handle_terminal(self, parallel):
        print(parallel, 'terminal')

        # Increment episode counter
        self.episodes += 1

        # Update experiment statistics
        self.episode_rewards.append(self.episode_reward[parallel])
        self.episode_timesteps.append(self.episode_timestep[parallel])
        if not self.join_agent_calls:  # !!!!!!
            self.episode_seconds.append(time.time() - self.episode_start[parallel])
            self.episode_agent_seconds.append(self.episode_agent_second[parallel])

        # Maximum number of episodes or episode callback (after counter increment!)
        if self.episodes >= self.num_episodes or (
            self.episodes % self.callback_episode_frequency == 0 and
            not self.callback(self, parallel)
        ):
            self.finished = True

        # Reset episode statistics
        self.episode_reward[parallel] = 0.0
        self.episode_timestep[parallel] = 0
        if not self.join_agent_calls:  # !!!!!!
            self.episode_agent_second[parallel] = 0.0
            self.episode_start[parallel] = time.time()

        # Reset environment
        if not self.finished and not self.sync_episodes:
            self.environments[parallel].start_reset()

    def handle_terminal_evaluation(self):
        print('terminal eval')

        # Update experiment statistics
        self.evaluation_rewards.append(self.evaluation_reward)
        self.evaluation_timesteps.append(self.evaluation_timestep)
        self.evaluation_seconds.append(time.time() - evaluation_start)
        self.evaluation_agent_seconds.append(self.evaluation_agent_second)

        # Evaluation callback
        if self.save_best_agent is not None:
            evaluation_score = self.evaluation_callback(self)
            assert isinstance(evaluation_score, float)
            if self.best_evaluation_score is None:
                self.best_evaluation_score = evaluation_score
            elif evaluation_score > self.best_evaluation_score:
                self.best_evaluation_score = evaluation_score
                self.agent.save(
                    directory=self.save_best_agent, filename='best-model', append_timestep=False
                )
        else:
            self.evaluation_callback(self)

        # Reset episode statistics
        self.evaluation_reward = 0.0
        self.evaluation_timestep = 0
        self.evaluation_agent_second = 0.0
        evaluation_start = time.time()

        # Reset environment
        if not self.finished and not self.sync_episodes:
            self.environments[parallel].start_reset()
