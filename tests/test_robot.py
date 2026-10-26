"""Tests for the simulated robot's gestures and response mapping."""

import asyncio

import pytest

from modules.robot import SimulatedRobot, gesture_for_response


def test_simulated_robot_tracks_gesture_and_rejects_unknown() -> None:
    async def exercise() -> None:
        robot = SimulatedRobot()
        await robot.speak("Hello!")
        await robot.gesture("wave")

        assert robot.is_connected()
        assert robot.last_text == "Hello!"
        assert robot.last_gesture == "wave"
        with pytest.raises(ValueError, match="Unsupported"):
            await robot.gesture("launch")

    asyncio.run(exercise())


def test_gesture_mapping_covers_response_types() -> None:
    assert gesture_for_response("anchoring_script") == "point"
    assert gesture_for_response("story_request") == "expressive"
    assert gesture_for_response("closing") == "bow"
    assert gesture_for_response("chat_question", "robot_command", "please bow") == "bow"
