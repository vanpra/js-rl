import copy
from enum import IntEnum
import logging
import os
from pathlib import Path
import pickle
import random
import time
from typing import Any, Optional

import gymnasium as gym
from gymnasium import spaces
import torch
from js_ast.analysis import count_statements
from js_ast.nodes import Node
import numpy as np
from rl.program_state import ProgramState
from tqdm import tqdm
from rl.tokenizer import ASTTokenizer

from utils.js_engine import Coverage
from utils.js_engine import Engine
from utils.js_engine import ExecutionData


STATEMENT_PENALTY_WEIGHT = 5


class FuzzingAction(IntEnum):
    REPLACE = 0
    ADD = 1
    REMOVE = 2
    MOVE_UP = 3
    MOVE_DOWN = 4
    END = 5

    def __str__(self):
        match self:
            case FuzzingAction.REPLACE:
                return "Replace"
            case FuzzingAction.ADD:
                return "Add"
            case FuzzingAction.REMOVE:
                return "Remove"
            case FuzzingAction.MOVE_UP:
                return "Move Up"
            case FuzzingAction.MOVE_DOWN:
                return "Move Down"
            case FuzzingAction.END:
                return "End"


class FuzzingEnv(gym.Env[tuple[Node, Node], np.int64]):
    metadata = {}

    def __init__(
        self,
        corpus: list[ProgramState],
        subtrees: dict[str, list[Node]],
        engine: Engine,
        total_coverage: Coverage,
        tokenizer: ASTTokenizer,
        interesting_folder: Path,
        max_mutations: int = 25,
        max_statements: int = 100,
        render_mode: Optional[str] = None,
    ):
        self.action_space = spaces.Discrete(len(FuzzingAction))
        # self.observation_space = spaces.Tuple(
        #     (
        #         spaces.Text(max_length=10000),
        #         spaces.Text(max_length=10000),
        #     )
        # )
        self.render_mode = render_mode

        self.corpus = corpus
        self.subtrees = subtrees
        self.engine = engine

        self.interesting_folder = interesting_folder
        os.makedirs(self.interesting_folder, exist_ok=True)

        self._state: ProgramState
        self.num_mutations = 0  # number of mutations performed
        self.max_mutations = max_mutations  # max number of mutations to perform
        self.max_statements = max_statements  # max number of statements in a program

        self.total_coverage = total_coverage
        self.coverage_increased = False  # whether coverage has increased

        self.total_executions = 0
        self.total_actions = 0

        self.tokenizer = tokenizer

    def save_current_state(self, save_type: str):
        current_time = int(time.time())

        code = self._state.generate_program_code()
        if code is None:
            return

        with open(self.interesting_folder / f"{current_time}_{save_type}.js", "w") as f:
            f.write(code)

        with open(
            self.interesting_folder / f"{current_time}_{save_type}.ast", "wb"
        ) as f:
            pickle.dump(self._state.program, f)

    def _get_obs(self) -> tuple[torch.Tensor, torch.Tensor]:
        tokenized_target = self.tokenizer.tokenize(self._state.get_target_node())
        tokenized_context = self.tokenizer.tokenize(self._state.get_context_node())
        return (tokenized_target, tokenized_context)

    def _get_info(self) -> dict[str, str]:
        return {}

    def _get_reward(self, exec_data: ExecutionData):
        num_statements = count_statements(self._state.program)
        penalty = (
            min(0, 1 - num_statements / self.max_statements) * STATEMENT_PENALTY_WEIGHT
        )

        new_coverage = exec_data.coverage | self.total_coverage

        if exec_data.is_crash():
            self.total_coverage = new_coverage
            logging.info(f"Crash detected: {exec_data.out}")
            self.save_current_state("crash")
            return 20 + penalty

        # new coverage is the same as the current coverage
        if new_coverage == self.total_coverage:
            return (
                exec_data.coverage.hit_edges / self.total_coverage.hit_edges + penalty
            )

        # new coverage has increased total coverage
        self.coverage_increased = True
        logging.info(f"Coverage increased from {self.total_coverage} to {new_coverage}")
        self.save_current_state("coverage")
        self.corpus.append(self._state)
        self.total_coverage = new_coverage

        return 10 + penalty

    def _get_done(self, exec_data: Optional[ExecutionData] = None) -> bool:
        return exec_data is not None and exec_data.is_crash()

    def _get_truncated(self) -> bool:
        return self.num_mutations >= self.max_mutations

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], dict[str, Any]]:
        # We need the following line to seed self.np_random
        super().reset(seed=seed, options=options)

        # Choose the agent's location uniformly at random
        self._state = copy.deepcopy(random.choice(self.corpus))
        # Initialise state as random child of the root node
        self._state.move_down()
        self.num_mutations = 0
        self.coverage_increased = False

        logging.info("Starting new episode")

        observation = self._get_obs()
        info = self._get_info()

        return observation, info

    def step(
        self, action: np.int64
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], float, bool, bool, dict[str, Any]]:
        logging.info(
            f"Number of mutations: {self.num_mutations}, action: {FuzzingAction(action)}"
        )
        self.total_actions += 1
        new_node = self._state.target_node

        match (action):
            case FuzzingAction.MOVE_UP:
                return self._move_up()
            case FuzzingAction.MOVE_DOWN:
                return self._move_down()
            case FuzzingAction.END:
                return self._end()
            case FuzzingAction.REPLACE:
                new_node = self._state.replace(self.subtrees)
            case FuzzingAction.ADD:
                new_node = self._state.add(self.subtrees)
            case FuzzingAction.REMOVE:
                new_node = self._state.remove()
            case _:
                raise ValueError(f"Invalid action: {action}")
        self.num_mutations += 1
        if new_node is self._state.target_node:
            # Negative reward for action which does not change the state
            return (
                self._get_obs(),
                -1,
                self._get_truncated(),
                self._get_done(),
                self._get_info(),
            )

        self._state.target_node = new_node
        code = self._state.generate_program_code()
        if not code:
            return (
                self._get_obs(),
                -1,
                self._get_truncated(),
                self._get_done(),
                self._get_info(),
            )
        exec_data = self.engine.execute_text(code)
        self.total_executions += 1

        if not exec_data:
            return self._get_obs(), 0, self._get_truncated(), True, self._get_info()

        self._state.coverage = exec_data.coverage
        reward = self._get_reward(exec_data)
        done = self._get_done(exec_data)

        return self._get_obs(), reward, self._get_truncated(), done, self._get_info()

    def _move_up(
        self,
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], float, bool, bool, dict[str, Any]]:
        return (
            self._get_obs(),
            0 if self._state.move_up() else -1,
            self._get_truncated(),
            self._get_done(),
            self._get_info(),
        )

    def _move_down(
        self,
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], float, bool, bool, dict[str, Any]]:
        return (
            self._get_obs(),
            0 if self._state.move_down() else -1,
            self._get_truncated(),
            self._get_done(),
            self._get_info(),
        )

    def _end(
        self,
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], float, bool, bool, dict[str, Any]]:
        return (
            self._get_obs(),
            0 if self.coverage_increased else -5,
            True,
            self._get_done(),
            self._get_info(),
        )
