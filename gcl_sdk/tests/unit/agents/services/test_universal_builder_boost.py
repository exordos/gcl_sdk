#    Copyright 2025 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from unittest import mock

import pytest

from gcl_sdk.agents.universal.services import builder as builder_svc


class DummyBoostBuilder(builder_svc.UniversalBuilderService):
    """Builder which simulates work without a database.

    The service emulates the behavior of the real `_iteration`: it
    performs some "work" and applies the boost state afterwards.
    """

    def __init__(self, **kwargs):
        super().__init__(instance_model=mock.MagicMock(), **kwargs)
        self.simulated_work = False

    def _setup(self):
        pass

    def _iteration(self):
        self._boost_work_performed = self.simulated_work
        self._apply_boost_state()


class TestBuilderBoostMode:
    def test_work_enables_boost(self):
        svc = DummyBoostBuilder()
        svc.simulated_work = True

        svc._loop_iteration()

        assert svc.is_boosted
        assert svc.effective_iter_min_period == 0.5
        assert svc.boost_remaining_iterations == 4

    def test_no_work_keeps_default_pace(self):
        svc = DummyBoostBuilder()

        svc._loop_iteration()

        assert not svc.is_boosted
        assert svc.effective_iter_min_period == 3

    def test_boost_expires_without_work(self):
        svc = DummyBoostBuilder(boost_iterations=3)
        svc.simulated_work = True
        svc._loop_iteration()
        assert svc.boost_remaining_iterations == 2

        # The boost expires after 3 boosted iterations in total
        svc.simulated_work = False

        svc._loop_iteration()
        assert svc.is_boosted

        svc._loop_iteration()
        assert not svc.is_boosted
        assert svc.effective_iter_min_period == 3

    def test_repeated_work_refreshes_boost(self):
        svc = DummyBoostBuilder(boost_iterations=3)
        svc.simulated_work = True

        for _ in range(10):
            svc._loop_iteration()
            assert svc.is_boosted
            assert svc.effective_iter_min_period == 0.5

    def test_overheat_protection_is_enabled_by_default(self):
        svc = DummyBoostBuilder()

        assert svc._max_boost_iterations == 100
        assert svc._boost_cooldown_iterations == 200

    def test_collection_inherits_boost_parameters(self):
        svc = builder_svc.CollectionUniversalBuilderService(
            instance_models=[mock.MagicMock()],
            boost_period=0.25,
            boost_iterations=7,
            boost_max_iterations=42,
            boost_cooldown_iterations=99,
        )

        assert svc._boost_period == 0.25
        assert svc._boost_iterations == 7
        assert svc._max_boost_iterations == 42
        assert svc._boost_cooldown_iterations == 99

    def test_collection_constructor_rejects_empty_models(self):
        with pytest.raises(ValueError):
            builder_svc.CollectionUniversalBuilderService(instance_models=[])


class TestBuilderBoostOverheat:
    def test_overheat_forces_cooldown(self):
        svc = DummyBoostBuilder(
            boost_iterations=10,
            boost_max_iterations=3,
            boost_cooldown_iterations=2,
        )
        svc.simulated_work = True

        # A buggy logic reports work on every iteration
        for _ in range(3):
            svc._loop_iteration()

        # Boost overheated - the service cools down in the default pace
        assert svc.is_cooling_down
        assert not svc.is_boosted
        assert svc.effective_iter_min_period == 3
        assert svc.boost_overheat_count == 1

        # Boost is refused during the cooldown
        svc._loop_iteration()
        assert svc.is_cooling_down
        assert not svc.is_boosted
        svc._loop_iteration()
        assert not svc.is_cooling_down
        assert not svc.is_boosted

        # The boost is available again
        svc._loop_iteration()
        assert svc.is_boosted
        assert svc.effective_iter_min_period == 0.5

    def test_overheat_does_not_break_normal_work(self):
        # The service does real work but stops in time - no overheating
        svc = DummyBoostBuilder(
            boost_iterations=3,
            boost_max_iterations=10,
            boost_cooldown_iterations=100,
        )

        for _ in range(4):
            svc.simulated_work = True
            svc._loop_iteration()

        svc.simulated_work = False
        for _ in range(3):
            svc._loop_iteration()

        assert not svc.is_boosted
        assert not svc.is_cooling_down
        assert svc.boost_overheat_count == 0

    def test_protection_can_be_disabled(self):
        svc = DummyBoostBuilder(
            boost_max_iterations=None,
            boost_cooldown_iterations=None,
        )
        svc.simulated_work = True

        for _ in range(50):
            svc._loop_iteration()
            assert svc.is_boosted

        assert not svc.is_cooling_down
