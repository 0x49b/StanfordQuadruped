from stanford_quadruped_controller import gait_controller
from stanford_quadruped_controller import stance_controller
from stanford_quadruped_controller import swing_leg_controller
from stanford_quadruped_controller import utilities
from stanford_quadruped_controller import state
from stanford_quadruped_controller import command
from stanford_quadruped_controller import config

import numpy as np
from transforms3d.euler import euler2mat, quat2euler
from transforms3d.quaternions import qconjugate, quat2axangle
from transforms3d.axangles import axangle2mat
from typing import Any, Tuple


REST = state.BehaviorState.REST
HOP = state.BehaviorState.HOP
FINISHHOP = state.BehaviorState.FINISHHOP
TROT = state.BehaviorState.TROT
DEACTIVATED = state.BehaviorState.DEACTIVATED


class Controller:
    """Controller and planner object
    """

    def __init__(self, config: config.Configuration, inverse_kinematics) -> None:
        self.config = config

        self.smoothed_yaw = 0.0  # for REST mode only
        self.inverse_kinematics = inverse_kinematics

        self.contact_modes = np.zeros(4)
        self.gait_controller = gait_controller.GaitController(self.config)
        self.swing_controller = swing_leg_controller.SwingController(self.config)
        self.stance_controller = stance_controller.StanceController(self.config)

        self.hop_transition_mapping = {
            REST: HOP,
            HOP: FINISHHOP,
            FINISHHOP: REST,
            TROT: HOP,
        }
        self.trot_transition_mapping = {
            REST: TROT,
            TROT: REST,
            HOP: TROT,
            FINISHHOP: TROT,
        }
        self.activate_transition_mapping = {
            DEACTIVATED: REST,
            REST: DEACTIVATED,
        }

    def step_gait(
        self, state: state.State, command: command.Command
    ) -> Tuple[Any, Any]:
        """Calculate the desired foot locations for the next timestep

        Returns
        -------
        Numpy array (3, 4)
            Matrix of new foot locations.
        """
        contact_modes = self.gait_controller.contacts(state.ticks)
        new_foot_locations = np.zeros((3, 4))
        for leg_index in range(4):
            contact_mode = contact_modes[leg_index]
            foot_location = state.foot_locations[:, leg_index]
            if contact_mode == 1:
                new_location = self.stance_controller.next_foot_location(
                    leg_index, state, command
                )
            else:
                swing_proportion = (
                    self.gait_controller.subphase_ticks(state.ticks)
                    / self.config.swing_ticks
                )
                new_location = self.swing_controller.next_foot_location(
                    swing_proportion, leg_index, state, command
                )
            new_foot_locations[:, leg_index] = new_location
        return new_foot_locations, contact_modes

    def run(self, state: state.State, command: command.Command) -> None:
        """Steps the controller forward one timestep

        Parameters
        ----------
        controller : Controller
            Robot controller object.
        """

        ########## Update operating state based on command ######
        if command.activate_event:
            state.behavior_state = self.activate_transition_mapping[
                state.behavior_state
            ]
        elif command.trot_event:
            state.behavior_state = self.trot_transition_mapping[state.behavior_state]
        elif command.hop_event:
            state.behavior_state = self.hop_transition_mapping[state.behavior_state]

        if state.behavior_state == TROT:
            state.foot_locations, contact_modes = self.step_gait(state, command)
            # TODO: add a state.final_foot_locations so we can track the rotated foot locations without compounding
            # Apply the desired body rotation
            rotated_foot_locations = (
                euler2mat(command.roll, command.pitch, 0.0) @ state.foot_locations
            )

            # Construct foot rotation matrix to compensate for body tilt
            (roll, pitch, yaw) = quat2euler(state.quat_orientation)
            correction_factor = 0.8
            max_tilt = 0.4
            roll_compensation = correction_factor * np.clip(roll, -max_tilt, max_tilt)
            pitch_compensation = correction_factor * np.clip(pitch, -max_tilt, max_tilt)
            rmat = euler2mat(roll_compensation, pitch_compensation, 0)

            rotated_foot_locations = rmat.T @ rotated_foot_locations

            state.joint_angles = self.inverse_kinematics(
                rotated_foot_locations, self.config
            )

        elif state.behavior_state == HOP:
            state.foot_locations = (
                self.config.default_stance + np.array([0, 0, -0.09])[:, np.newaxis]
            )
            state.joint_angles = self.inverse_kinematics(
                state.foot_locations, self.config
            )

        elif state.behavior_state == FINISHHOP:
            state.foot_locations = (
                self.config.default_stance + np.array([0, 0, -0.22])[:, np.newaxis]
            )
            state.joint_angles = self.inverse_kinematics(
                state.foot_locations, self.config
            )

        elif state.behavior_state == REST:
            yaw_proportion = command.yaw_rate / self.config.max_yaw_rate
            self.smoothed_yaw += self.config.dt * utilities.clipped_first_order_filter(
                self.smoothed_yaw,
                yaw_proportion * -self.config.max_stance_yaw,
                self.config.max_stance_yaw_rate,
                self.config.yaw_time_constant,
            )
            # Set the foot locations to the default stance plus the standard height
            state.foot_locations = (
                self.config.default_stance
                + np.array([0, 0, command.height])[:, np.newaxis]
            )
            # Apply the desired body rotation
            rotated_foot_locations = (
                euler2mat(command.roll, command.pitch, self.smoothed_yaw)
                @ state.foot_locations
            )
            state.joint_angles = self.inverse_kinematics(
                rotated_foot_locations, self.config
            )

        state.ticks += 1
        state.pitch = command.pitch
        state.roll = command.roll
        state.height = command.height
