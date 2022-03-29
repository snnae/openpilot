from cereal import car
from common.realtime import DT_CTRL
from common.numpy_fast import clip
from common.conversions import Conversions as CV
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfahda_mfc, create_acc_commands, create_acc_opt, create_frt_radar_opt
from selfdrive.car.hyundai.values import Buttons, CarControllerParams, CAR
from opendbc.can.packer import CANPacker

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState


def process_hud_alert(enabled, fingerprint, hud_control):
  sys_warning = (hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  sys_state = 1
  if hud_control.leftLaneVisible and hud_control.rightLaneVisible or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif hud_control.leftLaneVisible:
    sys_state = 5
  elif hud_control.rightLaneVisible:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if hud_control.leftLaneDepart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2
  if hud_control.rightLaneDepart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)
    self.frame = 0

    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.steer_rate_limited = False
    self.last_resume_frame = 0
    self.accel = 0

  def update(self, CC, CS):
    actuators = CC.actuators
    hud_control = CC.hudControl
    pcm_cancel_cmd = CC.cruiseControl.cancel

    # Steering Torque
    new_steer = int(round(actuators.steer * self.params.STEER_MAX))
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.params)
    self.steer_rate_limited = new_steer != apply_steer

    if not CC.latActive:
      apply_steer = 0

    self.apply_steer_last = apply_steer

    sys_warning, sys_state, left_lane_warning, right_lane_warning = process_hud_alert(CC.enabled, self.car_fingerprint,
                                                                                      hud_control)

    can_sends = []

    # tester present - w/ no response (keeps radar disabled)
    if self.CP.openpilotLongitudinalControl:
      if self.frame % 100 == 0:
        can_sends.append([0x7D0, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 0])

    can_sends.append(create_lkas11(self.packer, self.frame, self.car_fingerprint, apply_steer, CC.latActive,
                                   CS.lkas11, sys_warning, sys_state, CC.enabled,
                                   hud_control.leftLaneVisible, hud_control.rightLaneVisible,
                                   left_lane_warning, right_lane_warning))

    if not self.CP.openpilotLongitudinalControl:
      if pcm_cancel_cmd:
        can_sends.append(create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL))
      elif CS.out.cruiseState.standstill:
        # send resume at a max freq of 10Hz
        if (self.frame - self.last_resume_frame) * DT_CTRL > 0.1:
          # send 25 messages at a time to increases the likelihood of resume being accepted
          can_sends.extend([create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL)] * 25)
          self.last_resume_frame = self.frame

    if self.frame % 2 == 0 and self.CP.openpilotLongitudinalControl:
      pid_control = (actuators.longControlState == LongCtrlState.pid)
      stopping = (actuators.longControlState == LongCtrlState.stopping)
      accel = actuators.accel if CC.longActive else 0
      # TODO: aEgo lags when jerk is high, use smoothed ACCEL_REF_ACC instead?
      accel_error = accel - CS.out.aEgo if CC.longActive else 0

      # TODO: jerk upper would probably be better from longitudinal planner desired jerk?
      jerk_upper = clip(2.0 * accel_error, 0.0, 2.0) # zero when error is negative to keep decel control tight
      jerk_lower = 12.7 # always max value to keep decel control tight

      if pid_control and CS.out.standstill and CS.brake_control_active and accel > 0.0:
        # brake controller needs to wind up internallly until it reaches a threshhold where the brakes release
        # larger values cause faster windup (too small and you never start moving)
        # larger values get vehicle moving quicker but can cause sharp negative jerk transitioning back to plan
        # larger values also cause more overshoot and therefore it takes longer to stop once moving
        accel = 1.0
        jerk_upper = 1.0

      accel = clip(accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)

      set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_MPH if CS.clu11["CF_Clu_SPEED_UNIT"] == 1 else CV.MS_TO_KPH)
      can_sends.extend(create_acc_commands(self.packer, CC.enabled, accel, jerk_upper, jerk_lower, int(self.frame / 2),
                                           hud_control.leadVisible, set_speed_in_units, stopping, CS.out.gasPressed))
      self.accel = accel

    # 20 Hz LFA MFA message
    if self.frame % 5 == 0 and self.car_fingerprint in (CAR.SONATA, CAR.PALISADE, CAR.IONIQ, CAR.KIA_NIRO_EV, CAR.KIA_NIRO_HEV_2021,
                                                        CAR.IONIQ_EV_2020, CAR.IONIQ_PHEV, CAR.KIA_CEED, CAR.KIA_SELTOS, CAR.KONA_EV,
                                                        CAR.ELANTRA_2021, CAR.ELANTRA_HEV_2021, CAR.SONATA_HYBRID, CAR.KONA_HEV, CAR.SANTA_FE_2022,
                                                        CAR.KIA_K5_2021, CAR.IONIQ_HEV_2022, CAR.SANTA_FE_HEV_2022, CAR.GENESIS_G70_2020, CAR.SANTA_FE_PHEV_2022):
      can_sends.append(create_lfahda_mfc(self.packer, CC.enabled))

    # 5 Hz ACC options
    if self.frame % 20 == 0 and self.CP.openpilotLongitudinalControl:
      can_sends.extend(create_acc_opt(self.packer))

    # 2 Hz front radar options
    if self.frame % 50 == 0 and self.CP.openpilotLongitudinalControl:
      can_sends.append(create_frt_radar_opt(self.packer))

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / self.params.STEER_MAX
    new_actuators.accel = self.accel

    self.frame += 1
    return new_actuators, can_sends
