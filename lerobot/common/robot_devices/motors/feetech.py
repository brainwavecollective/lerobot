import enum
import logging
import math
import time
import traceback
from copy import deepcopy
from pathlib import Path

import numpy as np
import tqdm
from scservo_sdk import (
    COMM_SUCCESS,
    SCS_HIBYTE,
    SCS_HIWORD,
    SCS_LOBYTE,
    SCS_LOWORD,
    GroupSyncRead,
    GroupSyncWrite,
    PacketHandler,
    PortHandler,
)

from lerobot.common.robot_devices.utils import RobotDeviceAlreadyConnectedError, RobotDeviceNotConnectedError
from lerobot.common.utils.utils import capture_timestamp_utc

PROTOCOL_VERSION = 0
BAUDRATE = 1_000_000
TIMEOUT_MS = 1000

MAX_ID_RANGE = 252

# The following bounds define the lower and upper joints range (after calibration).
# For joints in degree (i.e. revolute joints), their nominal range is [-180, 180] degrees
# which corresponds to a half rotation on the left and half rotation on the right.
# Some joints might require higher range, so we allow up to [-270, 270] degrees until
# an error is raised.
LOWER_BOUND_DEGREE = -270
UPPER_BOUND_DEGREE = 270
# For joints in percentage (i.e. joints that move linearly like the prismatic joint of a gripper),
# their nominal range is [0, 100] %. For instance, for Aloha gripper, 0% is fully
# closed, and 100% is fully open. To account for slight calibration issue, we allow up to
# [-10, 110] until an error is raised.
LOWER_BOUND_LINEAR = -10
UPPER_BOUND_LINEAR = 110

HALF_TURN_DEGREE = 180


# data_name: (address, size_byte)
SCS_SERIES_CONTROL_TABLE = {
    "Model": (3, 2),
    "ID": (5, 1),
    "Baud_Rate": (6, 1),
    "Return_Delay": (7, 1),
    "Response_Status_Level": (8, 1),
    "Min_Angle_Limit": (9, 2),
    "Max_Angle_Limit": (11, 2),
    "Max_Temperature_Limit": (13, 1),
    "Max_Voltage_Limit": (14, 1),
    "Min_Voltage_Limit": (15, 1),
    "Max_Torque_Limit": (16, 2),
    "Phase": (18, 1),
    "Unloading_Condition": (19, 1),
    "LED_Alarm_Condition": (20, 1),
    "P_Coefficient": (21, 1),
    "D_Coefficient": (22, 1),
    "I_Coefficient": (23, 1),
    "Minimum_Startup_Force": (24, 2),
    "CW_Dead_Zone": (26, 1),
    "CCW_Dead_Zone": (27, 1),
    "Protection_Current": (28, 2),
    "Angular_Resolution": (30, 1),
    "Offset": (31, 2),
    "Mode": (33, 1),
    "Protective_Torque": (34, 1),
    "Protection_Time": (35, 1),
    "Overload_Torque": (36, 1),
    "Speed_closed_loop_P_proportional_coefficient": (37, 1),
    "Over_Current_Protection_Time": (38, 1),
    "Velocity_closed_loop_I_integral_coefficient": (39, 1),
    "Torque_Enable": (40, 1),
    "Acceleration": (41, 1),
    "Goal_Position": (42, 2),
    "Goal_Time": (44, 2),
    "Goal_Speed": (46, 2),
    "Lock": (55, 1),
    "Present_Position": (56, 2),
    "Present_Speed": (58, 2),
    "Present_Load": (60, 2),
    "Present_Voltage": (62, 1),
    "Present_Temperature": (63, 1),
    "Status": (65, 1),
    "Moving": (66, 1),
    "Present_Current": (69, 2),
}

SCS_SERIES_BAUDRATE_TABLE = {
    0: 1_000_000,
    1: 500_000,
    2: 250_000,
    3: 128_000,
    4: 115_200,
    5: 57_600,
    6: 38_400,
    7: 19_200,
}

CALIBRATION_REQUIRED = ["Goal_Position", "Present_Position"]
CONVERT_UINT32_TO_INT32_REQUIRED = ["Goal_Position", "Present_Position"]


MODEL_CONTROL_TABLE = {
    "scs_series": SCS_SERIES_CONTROL_TABLE,
    "sts3215": SCS_SERIES_CONTROL_TABLE,
}

MODEL_RESOLUTION = {
    "scs_series": 4096,
    "sts3215": 4096,
}

MODEL_BAUDRATE_TABLE = {
    "scs_series": SCS_SERIES_BAUDRATE_TABLE,
    "sts3215": SCS_SERIES_BAUDRATE_TABLE,
}

NUM_READ_RETRY = 10
NUM_WRITE_RETRY = 10


def convert_degrees_to_steps(degrees: float | np.ndarray, models: str | list[str]) -> np.ndarray:
    """This function converts the degree range to the step range for indicating motors rotation.
    It assumes a motor achieves a full rotation by going from -180 degree position to +180.
    The motor resolution (e.g. 4096) corresponds to the number of steps needed to achieve a full rotation.
    """
    resolutions = [MODEL_RESOLUTION[model] for model in models]
    steps = degrees / 180 * np.array(resolutions) / 2
    steps = steps.astype(int)
    return steps


def convert_to_bytes(value, bytes):
    # Note: No need to convert back into unsigned int, since this byte preprocessing
    # already handles it for us.
    if bytes == 1:
        data = [
            SCS_LOBYTE(SCS_LOWORD(value)),
        ]
    elif bytes == 2:
        data = [
            SCS_LOBYTE(SCS_LOWORD(value)),
            SCS_HIBYTE(SCS_LOWORD(value)),
        ]
    elif bytes == 4:
        data = [
            SCS_LOBYTE(SCS_LOWORD(value)),
            SCS_HIBYTE(SCS_LOWORD(value)),
            SCS_LOBYTE(SCS_HIWORD(value)),
            SCS_HIBYTE(SCS_HIWORD(value)),
        ]
    else:
        raise NotImplementedError(
            f"Value of the number of bytes to be sent is expected to be in [1, 2, 4], but "
            f"{bytes} is provided instead."
        )
    return data


def get_group_sync_key(data_name, motor_names):
    group_key = f"{data_name}_" + "_".join(motor_names)
    return group_key


def get_result_name(fn_name, data_name, motor_names):
    group_key = get_group_sync_key(data_name, motor_names)
    rslt_name = f"{fn_name}_{group_key}"
    return rslt_name


def get_queue_name(fn_name, data_name, motor_names):
    group_key = get_group_sync_key(data_name, motor_names)
    queue_name = f"{fn_name}_{group_key}"
    return queue_name


def get_log_name(var_name, fn_name, data_name, motor_names):
    group_key = get_group_sync_key(data_name, motor_names)
    log_name = f"{var_name}_{fn_name}_{group_key}"
    return log_name


def assert_same_address(model_ctrl_table, motor_models, data_name):
    all_addr = []
    all_bytes = []
    for model in motor_models:
        addr, bytes = model_ctrl_table[model][data_name]
        all_addr.append(addr)
        all_bytes.append(bytes)

    if len(set(all_addr)) != 1:
        raise NotImplementedError(
            f"At least two motor models use a different address for `data_name`='{data_name}' ({list(zip(motor_models, all_addr, strict=False))}). Contact a LeRobot maintainer."
        )

    if len(set(all_bytes)) != 1:
        raise NotImplementedError(
            f"At least two motor models use a different bytes representation for `data_name`='{data_name}' ({list(zip(motor_models, all_bytes, strict=False))}). Contact a LeRobot maintainer."
        )


def find_available_ports():
    ports = []
    for path in Path("/dev").glob("tty*"):
        ports.append(str(path))
    return ports


def find_port():
    print("Finding all available ports for the FeetechMotorsBus.")
    ports_before = find_available_ports()
    print(ports_before)

    print("Remove the usb cable from your FeetechMotorsBus and press Enter when done.")
    input()

    time.sleep(0.5)
    ports_after = find_available_ports()
    ports_diff = list(set(ports_before) - set(ports_after))

    if len(ports_diff) == 1:
        port = ports_diff[0]
        print(f"The port of this FeetechMotorsBus is '{port}'")
        print("Reconnect the usb cable.")
    elif len(ports_diff) == 0:
        raise OSError(f"Could not detect the port. No difference was found ({ports_diff}).")
    else:
        raise OSError(f"Could not detect the port. More than one port was found ({ports_diff}).")


class TorqueMode(enum.Enum):
    ENABLED = 1
    DISABLED = 0


class DriveMode(enum.Enum):
    NON_INVERTED = 0
    INVERTED = 1


class CalibrationMode(enum.Enum):
    # Joints with rotational motions are expressed in degrees in nominal range of [-180, 180]
    DEGREE = 0
    # Joints with linear motions (like gripper of Aloha) are experessed in nominal range of [0, 100]
    LINEAR = 1


class JointOutOfRangeError(Exception):
    def __init__(self, message="Joint is out of range"):
        self.message = message
        super().__init__(self.message)


class FeetechMotorsBus:
    # TODO(rcadene): Add a script to find the motor indices without feetechWizzard2
    """
    The FeetechMotorsBus class allows to efficiently read and write to the attached motors. It relies on
    the python feetech sdk to communicate with the motors. For more info, see the [feetech SDK Documentation](https://emanual.robotis.com/docs/en/software/feetech/feetech_sdk/sample_code/python_read_write_protocol_2_0/#python-read-write-protocol-20).

    A FeetechMotorsBus instance requires a port (e.g. `FeetechMotorsBus(port="/dev/tty.usbmodem575E0031751"`)).
    To find the port, you can run our utility script:
    ```bash
    python lerobot/common/robot_devices/motors/feetech.py
    >>> Finding all available ports for the FeetechMotorsBus.
    >>> ['/dev/tty.usbmodem575E0032081', '/dev/tty.usbmodem575E0031751']
    >>> Remove the usb cable from your FeetechMotorsBus and press Enter when done.
    >>> The port of this FeetechMotorsBus is /dev/tty.usbmodem575E0031751.
    >>> Reconnect the usb cable.
    ```

    Example of usage for 1 motor connected to the bus:
    ```python
    motor_name = "gripper"
    motor_index = 6
    motor_model = "xl330-m288"

    motors_bus = FeetechMotorsBus(
        port="/dev/tty.usbmodem575E0031751",
        motors={motor_name: (motor_index, motor_model)},
    )
    motors_bus.connect()

    position = motors_bus.read("Present_Position")

    # move from a few motor steps as an example
    few_steps = 30
    motors_bus.write("Goal_Position", position + few_steps)

    # when done, consider disconnecting
    motors_bus.disconnect()
    ```
    """

    def __init__(
        self,
        port: str,
        motors: dict[str, tuple[int, str]],
        extra_model_control_table: dict[str, list[tuple]] | None = None,
        extra_model_resolution: dict[str, int] | None = None,
    ):
        self.port = port
        self.motors = motors

        self.model_ctrl_table = deepcopy(MODEL_CONTROL_TABLE)
        if extra_model_control_table:
            self.model_ctrl_table.update(extra_model_control_table)

        self.model_resolution = deepcopy(MODEL_RESOLUTION)
        if extra_model_resolution:
            self.model_resolution.update(extra_model_resolution)

        self.port_handler = None
        self.packet_handler = None
        self.calibration = None
        self.is_connected = False
        self.group_readers = {}
        self.group_writers = {}
        self.logs = {}

    def connect(self):
        if self.is_connected:
            raise RobotDeviceAlreadyConnectedError(
                f"FeetechMotorsBus({self.port}) is already connected. Do not call `motors_bus.connect()` twice."
            )

        self.port_handler = PortHandler(self.port)
        self.packet_handler = PacketHandler(PROTOCOL_VERSION)

        try:
            if not self.port_handler.openPort():
                raise OSError(f"Failed to open port '{self.port}'.")
        except Exception:
            traceback.print_exc()
            print(
                "\nTry running `python lerobot/common/robot_devices/motors/feetech.py` to make sure you are using the correct port.\n"
            )
            raise

        # Allow to read and write
        self.is_connected = True

        self.port_handler.setPacketTimeoutMillis(TIMEOUT_MS)

        if not self.are_motors_configured():
            raise OSError(
                "Motors are not configured. Please use configure_motors.py to configure motors before continuing."
            )

    def reconnect(self):
        self.port_handler = PortHandler(self.port)
        self.packet_handler = PacketHandler(PROTOCOL_VERSION)
        if not self.port_handler.openPort():
            raise OSError(f"Failed to open port '{self.port}'.")
        self.is_connected = True

    def are_motors_configured(self):
        # Only check the motor indices and not baudrate, since if the motor baudrates are incorrect,
        # a ConnectionError will be raised anyway.
        try:
            return (self.motor_indices == self.read("ID")).all()
        except ConnectionError as e:
            print(e)
            return False

    def configure_motors(self):
        # TODO(rcadene): This script assumes motors follow the X_SERIES baudrates
        # TODO(rcadene): Refactor this function with intermediate high-level functions

        print("Scanning all baudrates and motor indices")
        all_baudrates = set(SCS_SERIES_BAUDRATE_TABLE.values())
        ids_per_baudrate = {}
        for baudrate in all_baudrates:
            self.set_bus_baudrate(baudrate)
            present_ids = self.find_motor_indices()
            if len(present_ids) > 0:
                ids_per_baudrate[baudrate] = present_ids
        print(f"Motor indices detected: {ids_per_baudrate}")
        print()

        possible_baudrates = list(ids_per_baudrate.keys())
        possible_ids = list({idx for sublist in ids_per_baudrate.values() for idx in sublist})
        untaken_ids = list(set(range(MAX_ID_RANGE)) - set(possible_ids) - set(self.motor_indices))

        # Connect successively one motor to the chain and write a unique random index for each
        for i in range(len(self.motors)):
            self.disconnect()
            input(
                "1. Unplug the power cord\n"
                "2. Plug/unplug minimal number of cables to only have the first "
                f"{i+1} motor(s) ({self.motor_names[:i+1]}) connected.\n"
                "3. Re-plug the power cord\n"
                "Press Enter to continue..."
            )
            print()
            self.reconnect()

            if i > 0:
                try:
                    self.read_with_motor_ids(self.motor_models, untaken_ids[:i], "ID")
                except ConnectionError:
                    print(f"Failed to read from {untaken_ids[:i+1]}. Make sure the power cord is plugged in.")
                    input("Press Enter to continue...")
                    print()
                    self.reconnect()

            print("Scanning possible baudrates and motor indices")
            motor_found = False
            for baudrate in possible_baudrates:
                self.set_bus_baudrate(baudrate)
                present_ids = self.find_motor_indices(possible_ids)
                if len(present_ids) == 1:
                    present_idx = present_ids[0]
                    print(f"Detected motor with index {present_idx}")

                    if baudrate != BAUDRATE:
                        print(f"Setting its baudrate to {BAUDRATE}")
                        baudrate_idx = list(SCS_SERIES_BAUDRATE_TABLE.values()).index(BAUDRATE)

                        # The write can fail, so we allow retries
                        for _ in range(NUM_WRITE_RETRY):
                            self.write_with_motor_ids(
                                self.motor_models, present_idx, "Baud_Rate", baudrate_idx
                            )
                            time.sleep(0.5)
                            self.set_bus_baudrate(BAUDRATE)
                            try:
                                present_baudrate_idx = self.read_with_motor_ids(
                                    self.motor_models, present_idx, "Baud_Rate"
                                )
                            except ConnectionError:
                                print("Failed to write baudrate. Retrying.")
                                self.set_bus_baudrate(baudrate)
                                continue
                            break
                        else:
                            raise

                        if present_baudrate_idx != baudrate_idx:
                            raise OSError("Failed to write baudrate.")

                    print(f"Setting its index to a temporary untaken index ({untaken_ids[i]})")
                    self.write_with_motor_ids(self.motor_models, present_idx, "ID", untaken_ids[i])

                    present_idx = self.read_with_motor_ids(self.motor_models, untaken_ids[i], "ID")
                    if present_idx != untaken_ids[i]:
                        raise OSError("Failed to write index.")

                    motor_found = True
                    break
                elif len(present_ids) > 1:
                    raise OSError(f"More than one motor detected ({present_ids}), but only one was expected.")

            if not motor_found:
                raise OSError(
                    "No motor found, but one new motor expected. Verify power cord is plugged in and retry."
                )
            print()

        print(f"Setting expected motor indices: {self.motor_indices}")
        self.set_bus_baudrate(BAUDRATE)
        self.write_with_motor_ids(
            self.motor_models, untaken_ids[: len(self.motors)], "ID", self.motor_indices
        )
        print()

        if (self.read("ID") != self.motor_indices).any():
            raise OSError("Failed to write motors indices.")

        print("Configuration is done!")

    def find_motor_indices(self, possible_ids=None):
        if possible_ids is None:
            possible_ids = range(MAX_ID_RANGE)

        indices = []
        for idx in tqdm.tqdm(possible_ids):
            try:
                present_idx = self.read_with_motor_ids(self.motor_models, [idx], "ID")[0]
            except ConnectionError:
                continue

            if idx != present_idx:
                # sanity check
                raise OSError(
                    "Motor index used to communicate through the bus is not the same as the one present in the motor memory. The motor memory might be damaged."
                )
            indices.append(idx)

        return indices

    def set_bus_baudrate(self, baudrate):
        present_bus_baudrate = self.port_handler.getBaudRate()
        if present_bus_baudrate != baudrate:
            print(f"Setting bus baud rate to {baudrate}. Previously {present_bus_baudrate}.")
            self.port_handler.setBaudRate(baudrate)

            if self.port_handler.getBaudRate() != baudrate:
                raise OSError("Failed to write bus baud rate.")

    @property
    def motor_names(self) -> list[str]:
        return list(self.motors.keys())

    @property
    def motor_models(self) -> list[str]:
        return [model for _, model in self.motors.values()]

    @property
    def motor_indices(self) -> list[int]:
        return [idx for idx, _ in self.motors.values()]

    def set_calibration(self, calibration: dict[str, list]):
        self.calibration = calibration

    def apply_calibration_autocorrect(self, values: np.ndarray | list, motor_names: list[str] | None):
        """This function apply the calibration, automatically detects out of range errors for motors values and attempt to correct.

        For more info, see docstring of `apply_calibration` and `autocorrect_calibration`.
        """
        try:
            values = self.apply_calibration(values, motor_names)
        except JointOutOfRangeError as e:
            print(e)
            self.autocorrect_calibration(values, motor_names)
            values = self.apply_calibration(values, motor_names)
        return values

    def apply_calibration(self, values: np.ndarray | list, motor_names: list[str] | None):
        """Convert from unsigned int32 joint position range [0, 2**32[ to the universal float32 nominal degree range ]-180.0, 180.0[ with
        a "zero position" at 0 degree.

        Note: We say "nominal degree range" since the motors can take values outside this range. For instance, 190 degrees, if the motor
        rotate more than a half a turn from the zero position. However, most motors can't rotate more than 180 degrees and will stay in this range.

        Joints values are original in [0, 2**32[ (unsigned int32). Each motor are expected to complete a full rotation
        when given a goal position that is + or - their resolution. For instance, feetech xl330-m077 have a resolution of 4096, and
        at any position in their original range, let's say the position 56734, they complete a full rotation clockwise by moving to 60830,
        or anticlockwise by moving to 52638. The position in the original range is arbitrary and might change a lot between each motor.
        To harmonize between motors of the same model, different robots, or even models of different brands, we propose to work
        in the centered nominal degree range ]-180, 180[.
        """
        if motor_names is None:
            motor_names = self.motor_names

        # Convert from unsigned int32 original range [0, 2**32] to signed float32 range
        values = values.astype(np.float32)

        for i, name in enumerate(motor_names):
            calib_idx = self.calibration["motor_names"].index(name)
            calib_mode = self.calibration["calib_mode"][calib_idx]

            if CalibrationMode[calib_mode] == CalibrationMode.DEGREE:
                drive_mode = self.calibration["drive_mode"][calib_idx]
                homing_offset = self.calibration["homing_offset"][calib_idx]
                _, model = self.motors[name]
                resolution = self.model_resolution[model]

                # Update direction of rotation of the motor to match between leader and follower.
                # In fact, the motor of the leader for a given joint can be assembled in an
                # opposite direction in term of rotation than the motor of the follower on the same joint.
                if drive_mode:
                    values[i] *= -1

                # Convert from range [-2**31, 2**31[ to
                # nominal range ]-resolution, resolution[ (e.g. ]-2048, 2048[)
                values[i] += homing_offset

                # Convert from range ]-resolution, resolution[ to
                # universal float32 centered degree range ]-180, 180[
                values[i] = values[i] / (resolution // 2) * HALF_TURN_DEGREE

                if (values[i] < LOWER_BOUND_DEGREE) or (values[i] > UPPER_BOUND_DEGREE):
                    raise JointOutOfRangeError(
                        f"Wrong motor position range detected for {name}. "
                        f"Expected to be in nominal range of [-{HALF_TURN_DEGREE}, {HALF_TURN_DEGREE}] degrees (a full rotation), "
                        f"with a maximum range of [{LOWER_BOUND_DEGREE}, {UPPER_BOUND_DEGREE}] degrees to account for joints that can rotate a bit more, "
                        f"but present value is {values[i]} degree. "
                        "This might be due to a cable connection issue creating an artificial 360 degrees jump in motor values. "
                        "You need to recalibrate by running: `python lerobot/scripts/control_robot.py calibrate`"
                    )

            elif CalibrationMode[calib_mode] == CalibrationMode.LINEAR:
                start_pos = self.calibration["start_pos"][calib_idx]
                end_pos = self.calibration["end_pos"][calib_idx]

                # Rescale the present position to a nominal range [0, 100] %,
                # useful for joints with linear motions like Aloha gripper
                values[i] = (values[i] - start_pos) / (end_pos - start_pos) * 100

                if (values[i] < LOWER_BOUND_LINEAR) or (values[i] > UPPER_BOUND_LINEAR):
                    raise JointOutOfRangeError(
                        f"Wrong motor position range detected for {name}. "
                        f"Expected to be in nominal range of [0, 100] % (a full linear translation), "
                        f"with a maximum range of [{LOWER_BOUND_LINEAR}, {UPPER_BOUND_LINEAR}] % to account for some imprecision during calibration, "
                        f"but present value is {values[i]} %. "
                        "This might be due to a cable connection issue creating an artificial jump in motor values. "
                        "You need to recalibrate by running: `python lerobot/scripts/control_robot.py calibrate`"
                    )

        return values

    def autocorrect_calibration(self, values: np.ndarray | list, motor_names: list[str] | None):
        """This function automatically detects issues with values of motors after calibration, and correct for these issues.

        Some motors might have values outside of expected maximum bounds after calibration.
        For instance, for a joint in degree, its value can be outside [-270, 270] degrees, which is totally unexpected given
        a nominal range of [-180, 180] degrees, which represents half a turn to the left or right starting from zero position.

        Known issues:
        #1: Motor value randomly shifts of a full turn, caused by hardware/connection errors.
        #2: Motor internal homing offset is shifted of a full turn, caused by using default calibration (e.g Aloha).
        #3: motor internal homing offset is shifted of less or more than a full turn, caused by using default calibration
            or by human error during manual calibration.

        Issues #1 and #2 can be solved by shifting the calibration homing offset by a full turn.
        Issue #3 will be visually detected by user and potentially captured by the safety feature `max_relative_target`,
        that will slow down the motor, raise an error asking to recalibrate. Manual recalibrating will solve the issue.

        Note: A full turn corresponds to 360 degrees but also to 4096 steps for a motor resolution of 4096.
        """
        if motor_names is None:
            motor_names = self.motor_names

        # Convert from unsigned int32 original range [0, 2**32] to signed float32 range
        values = values.astype(np.float32)

        for i, name in enumerate(motor_names):
            calib_idx = self.calibration["motor_names"].index(name)
            calib_mode = self.calibration["calib_mode"][calib_idx]

            if CalibrationMode[calib_mode] == CalibrationMode.DEGREE:
                drive_mode = self.calibration["drive_mode"][calib_idx]
                homing_offset = self.calibration["homing_offset"][calib_idx]
                _, model = self.motors[name]
                resolution = self.model_resolution[model]

                if drive_mode:
                    values[i] *= -1

                # Convert from initial range to range [-180, 180] degrees
                calib_val = (values[i] + homing_offset) / (resolution // 2) * HALF_TURN_DEGREE
                in_range = (calib_val > LOWER_BOUND_DEGREE) and (calib_val < UPPER_BOUND_DEGREE)

                # Solve this inequality to find the factor to shift the range into [-180, 180] degrees
                # values[i] = (values[i] + homing_offset + resolution * factor) / (resolution // 2) * HALF_TURN_DEGREE
                # - HALF_TURN_DEGREE <= (values[i] + homing_offset + resolution * factor) / (resolution // 2) * HALF_TURN_DEGREE <= HALF_TURN_DEGREE
                # (- HALF_TURN_DEGREE / HALF_TURN_DEGREE * (resolution // 2) - values[i] - homing_offset) / resolution <= factor <= (HALF_TURN_DEGREE / 180 * (resolution // 2) - values[i] - homing_offset) / resolution
                low_factor = (
                    -HALF_TURN_DEGREE / HALF_TURN_DEGREE * (resolution // 2) - values[i] - homing_offset
                ) / resolution
                upp_factor = (
                    HALF_TURN_DEGREE / HALF_TURN_DEGREE * (resolution // 2) - values[i] - homing_offset
                ) / resolution

            elif CalibrationMode[calib_mode] == CalibrationMode.LINEAR:
                start_pos = self.calibration["start_pos"][calib_idx]
                end_pos = self.calibration["end_pos"][calib_idx]

                # Convert from initial range to range [0, 100] in %
                calib_val = (values[i] - start_pos) / (end_pos - start_pos) * 100
                in_range = (calib_val > LOWER_BOUND_LINEAR) and (calib_val < UPPER_BOUND_LINEAR)

                # Solve this inequality to find the factor to shift the range into [0, 100] %
                # values[i] = (values[i] - start_pos + resolution * factor) / (end_pos + resolution * factor - start_pos - resolution * factor) * 100
                # values[i] = (values[i] - start_pos + resolution * factor) / (end_pos - start_pos) * 100
                # 0 <= (values[i] - start_pos + resolution * factor) / (end_pos - start_pos) * 100 <= 100
                # (start_pos - values[i]) / resolution <= factor <= (end_pos - values[i]) / resolution
                low_factor = (start_pos - values[i]) / resolution
                upp_factor = (end_pos - values[i]) / resolution

            if not in_range:
                # Get first integer between the two bounds
                if low_factor < upp_factor:
                    factor = math.ceil(low_factor)

                    if factor > upp_factor:
                        raise ValueError(f"No integer found between bounds [{low_factor=}, {upp_factor=}]")
                else:
                    factor = math.ceil(upp_factor)

                    if factor > low_factor:
                        raise ValueError(f"No integer found between bounds [{low_factor=}, {upp_factor=}]")

                if CalibrationMode[calib_mode] == CalibrationMode.DEGREE:
                    out_of_range_str = f"{LOWER_BOUND_DEGREE} < {calib_val} < {UPPER_BOUND_DEGREE} degrees"
                    in_range_str = f"{LOWER_BOUND_DEGREE} < {calib_val} < {UPPER_BOUND_DEGREE} degrees"
                elif CalibrationMode[calib_mode] == CalibrationMode.LINEAR:
                    out_of_range_str = f"{LOWER_BOUND_LINEAR} < {calib_val} < {UPPER_BOUND_LINEAR} %"
                    in_range_str = f"{LOWER_BOUND_LINEAR} < {calib_val} < {UPPER_BOUND_LINEAR} %"

                logging.warning(
                    f"Auto-correct calibration of motor '{name}' by shifting value by {abs(factor)} full turns, "
                    f"from '{out_of_range_str}' to '{in_range_str}'."
                )

                # A full turn corresponds to 360 degrees but also to 4096 steps for a motor resolution of 4096.
                self.calibration["homing_offset"][calib_idx] += resolution * factor

    def revert_calibration(self, values: np.ndarray | list, motor_names: list[str] | None):
        """Inverse of `apply_calibration`."""
        if motor_names is None:
            motor_names = self.motor_names

        for i, name in enumerate(motor_names):
            calib_idx = self.calibration["motor_names"].index(name)
            calib_mode = self.calibration["calib_mode"][calib_idx]

            if CalibrationMode[calib_mode] == CalibrationMode.DEGREE:
                drive_mode = self.calibration["drive_mode"][calib_idx]
                homing_offset = self.calibration["homing_offset"][calib_idx]
                _, model = self.motors[name]
                resolution = self.model_resolution[model]

                # Convert from nominal 0-centered degree range [-180, 180] to
                # 0-centered resolution range (e.g. [-2048, 2048] for resolution=4096)
                values[i] = values[i] / HALF_TURN_DEGREE * (resolution // 2)

                # Substract the homing offsets to come back to actual motor range of values
                # which can be arbitrary.
                values[i] -= homing_offset

                # Remove drive mode, which is the rotation direction of the motor, to come back to
                # actual motor rotation direction which can be arbitrary.
                if drive_mode:
                    values[i] *= -1

            elif CalibrationMode[calib_mode] == CalibrationMode.LINEAR:
                start_pos = self.calibration["start_pos"][calib_idx]
                end_pos = self.calibration["end_pos"][calib_idx]

                # Convert from nominal lnear range of [0, 100] % to
                # actual motor range of values which can be arbitrary.
                values[i] = values[i] / 100 * (end_pos - start_pos) + start_pos

        values = np.round(values).astype(np.int32)
        return values

    def read_with_motor_ids(self, motor_models, motor_ids, data_name):
        return_list = True
        if not isinstance(motor_ids, list):
            return_list = False
            motor_ids = [motor_ids]

        assert_same_address(self.model_ctrl_table, self.motor_models, data_name)
        addr, bytes = self.model_ctrl_table[motor_models[0]][data_name]
        group = GroupSyncRead(self.port_handler, self.packet_handler, addr, bytes)
        for idx in motor_ids:
            group.addParam(idx)

        comm = group.txRxPacket()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Read failed due to communication error on port {self.port_handler.port_name} for indices {motor_ids}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

        values = []
        for idx in motor_ids:
            value = group.getData(idx, addr, bytes)
            values.append(value)

        if return_list:
            return values
        else:
            return values[0]

    def read(self, data_name, motor_names: str | list[str] | None = None):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                f"FeetechMotorsBus({self.port}) is not connected. You need to run `motors_bus.connect()`."
            )

        start_time = time.perf_counter()

        if motor_names is None:
            motor_names = self.motor_names

        if isinstance(motor_names, str):
            motor_names = [motor_names]

        motor_ids = []
        models = []
        for name in motor_names:
            motor_idx, model = self.motors[name]
            motor_ids.append(motor_idx)
            models.append(model)

        assert_same_address(self.model_ctrl_table, models, data_name)
        addr, bytes = self.model_ctrl_table[model][data_name]
        group_key = get_group_sync_key(data_name, motor_names)

        if data_name not in self.group_readers:
            # create new group reader
            self.group_readers[group_key] = GroupSyncRead(self.port_handler, self.packet_handler, addr, bytes)
            for idx in motor_ids:
                self.group_readers[group_key].addParam(idx)

        for _ in range(NUM_READ_RETRY):
            comm = self.group_readers[group_key].txRxPacket()
            if comm == COMM_SUCCESS:
                break

        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Read failed due to communication error on port {self.port} for group_key {group_key}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

        values = []
        for idx in motor_ids:
            value = self.group_readers[group_key].getData(idx, addr, bytes)
            values.append(value)

        values = np.array(values)

        # Convert to signed int to use range [-2048, 2048] for our motor positions.
        if data_name in CONVERT_UINT32_TO_INT32_REQUIRED:
            values = values.astype(np.int32)

        if data_name in CALIBRATION_REQUIRED and self.calibration is not None:
            values = self.apply_calibration_autocorrect(values, motor_names)

        # log the number of seconds it took to read the data from the motors
        delta_ts_name = get_log_name("delta_timestamp_s", "read", data_name, motor_names)
        self.logs[delta_ts_name] = time.perf_counter() - start_time

        # log the utc time at which the data was received
        ts_utc_name = get_log_name("timestamp_utc", "read", data_name, motor_names)
        self.logs[ts_utc_name] = capture_timestamp_utc()

        return values

    def write_with_motor_ids(self, motor_models, motor_ids, data_name, values):
        if not isinstance(motor_ids, list):
            motor_ids = [motor_ids]
        if not isinstance(values, list):
            values = [values]

        assert_same_address(self.model_ctrl_table, motor_models, data_name)
        addr, bytes = self.model_ctrl_table[motor_models[0]][data_name]
        group = GroupSyncWrite(self.port_handler, self.packet_handler, addr, bytes)
        for idx, value in zip(motor_ids, values, strict=True):
            data = convert_to_bytes(value, bytes)
            group.addParam(idx, data)

        comm = group.txPacket()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Write failed due to communication error on port {self.port_handler.port_name} for indices {motor_ids}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

    def write(self, data_name, values: int | float | np.ndarray, motor_names: str | list[str] | None = None):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                f"FeetechMotorsBus({self.port}) is not connected. You need to run `motors_bus.connect()`."
            )

        start_time = time.perf_counter()

        if motor_names is None:
            motor_names = self.motor_names

        if isinstance(motor_names, str):
            motor_names = [motor_names]

        if isinstance(values, (int, float, np.integer)):
            values = [int(values)] * len(motor_names)

        values = np.array(values)

        motor_ids = []
        models = []
        for name in motor_names:
            motor_idx, model = self.motors[name]
            motor_ids.append(motor_idx)
            models.append(model)

        if data_name in CALIBRATION_REQUIRED and self.calibration is not None:
            values = self.revert_calibration(values, motor_names)

        values = values.tolist()

        assert_same_address(self.model_ctrl_table, models, data_name)
        addr, bytes = self.model_ctrl_table[model][data_name]
        group_key = get_group_sync_key(data_name, motor_names)

        init_group = data_name not in self.group_readers
        if init_group:
            self.group_writers[group_key] = GroupSyncWrite(
                self.port_handler, self.packet_handler, addr, bytes
            )

        for idx, value in zip(motor_ids, values, strict=True):
            data = convert_to_bytes(value, bytes)
            if init_group:
                self.group_writers[group_key].addParam(idx, data)
            else:
                self.group_writers[group_key].changeParam(idx, data)

        comm = self.group_writers[group_key].txPacket()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Write failed due to communication error on port {self.port} for group_key {group_key}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

        # log the number of seconds it took to write the data to the motors
        delta_ts_name = get_log_name("delta_timestamp_s", "write", data_name, motor_names)
        self.logs[delta_ts_name] = time.perf_counter() - start_time

        # TODO(rcadene): should we log the time before sending the write command?
        # log the utc time when the write has been completed
        ts_utc_name = get_log_name("timestamp_utc", "write", data_name, motor_names)
        self.logs[ts_utc_name] = capture_timestamp_utc()

    def disconnect(self):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                f"FeetechMotorsBus({self.port}) is not connected. Try running `motors_bus.connect()` first."
            )

        if self.port_handler is not None:
            self.port_handler.closePort()
            self.port_handler = None

        self.packet_handler = None
        self.group_readers = {}
        self.group_writers = {}
        self.is_connected = False

    def __del__(self):
        if getattr(self, "is_connected", False):
            self.disconnect()


if __name__ == "__main__":
    # Helper to find the usb port associated to all your FeetechMotorsBus.
    find_port()