from __future__ import annotations

import datetime

import time
import appdaemon.plugins.hass.hassapi as hass
import threading
import appdaemon.plugins.mqtt.mqttapi as mqttapi
import pytz
from pydantic_settings import BaseSettings
from typing import Optional, List, Any, Union
import sys
from expiringdict import ExpiringDict
from appdaemon.__main__ import main
from appdaemon.appdaemon import AppDaemon
import collections

if __name__ == '__main__':
    sys.argv.extend(['-c', '../'])
    sys.exit(main())


class FanConfig(BaseSettings):
    power_attribute: str
    power_switch: str
    remote: str
    up_command: str
    down_command: str
    power_jitter: float
    power_values: List[int]
    name: Optional[str] = None


class config(BaseSettings):
    fans: dict[str, FanConfig]
    discovery_topic: str
    app_topic: str


class RestartableTimer():
    def __init__(self, hass: hass.Hass, callback: callable, interval: int, *args, **kwargs):
        self._hass = hass
        self._callback = callback
        self._interval = interval
        self._args = args
        self._kwargs = kwargs
        self._timer = None

    def start(self):
        if self._timer is not None:
            if self._hass.timer_running(self._timer):
                self._hass.cancel_timer(self._timer)
        self._timer = self._hass.run_in(self._onStart, self._interval)

    def _onStart(self, *args) -> Any:
        return self._callback(*self._args, **self._kwargs)


class ha_ir_fan(hass.Hass):
    class irFanItem:

        class Command:
            def __init__(self, command, data=None):
                self.command = command
                self.data = data
                self.dt = datetime.datetime.now().astimezone(pytz.UTC)

        def __init__(self, name: str, app_config: config, fan_config: FanConfig, app: ha_ir_fan):
            import json
            self.config = app_config
            self.fanConfig = fan_config
            self.app = app
            self.name = name
            self.state = True
            self.percentage = 0
            self.power = None
            self.power_updated = None
            self.power_history = ExpiringDict(max_len=100, max_age_seconds=5)
            self.power_timer: RestartableTimer = RestartableTimer(self.app, self.update_power_state, 1)
            self.power_state_mutex = threading.Lock()
            self.apply_mutex = threading.Lock()
            self.apply_speed_sent = None
            self.command_queue: collections.deque[ha_ir_fan.irFanItem.Command] = collections.deque()
            prev = 0
            self.state_ranges = []
            for x in sorted(self.fanConfig.power_values):
                self.state_ranges.append((prev, x))
                prev = x + 1
            self.app.mq.mqtt_publish(app_config.discovery_topic + '/fan/' + f'{self.app.name}_{self.name}/config',
                                     json.dumps(self.discovery))
            self.app.mq.listen_event(self.mqtt_event, "MQTT_MESSAGE")
            self.update_power_state()
            self.publish_state()
            self.app.run_every(self.update_power_state, datetime.datetime.now().astimezone(pytz.UTC), 1)
            self.app.run_every(self.apply_command, datetime.datetime.now().astimezone(pytz.UTC), 1)

        def has_power_jitter(self) -> bool:
            if len(self.power_history.values()) > 0:
                power_min = min(self.power_history.values()) + 1
                power_max = max(self.power_history.values()) + 1
                diff = power_max / power_min - 1
                return diff > self.fanConfig.power_jitter
            return False

        def update_power_state(self, args=None, kwargs=None):
            if self.power_state_mutex.acquire(blocking=False):
                try:
                    ent = self.app.get_entity(self.fanConfig.power_switch)
                    power = ent.get_state(attribute=self.fanConfig.power_attribute)
                    self.power_history[datetime.datetime.fromisoformat(ent.get_state(attribute='last_updated'))] = power
                    if not self.has_power_jitter():
                        power_updated = False
                        if self.power != power:
                            power_updated = True
                        self.power = power
                        self.power_updated = datetime.datetime.fromisoformat(
                            ent.get_state(attribute='last_updated')).astimezone(pytz.UTC)
                        if power_updated:
                            self.publish_state()
                            self.app.log("Publishing state")
                finally:
                    self.power_state_mutex.release()

        @property
        def speed(self) -> Union[int, None]:
            if self.power is None:
                return None
            power = self.power
            z = 0
            for x in self.state_ranges:
                if x[0] <= power <= x[1]:
                    break
                z += 1
            return z

        def publish_state(self):
            self.app.mq.mqtt_publish(self.discovery['state_topic'], 'ON' if self.speed > 0 else 'OFF')
            self.app.mq.mqtt_publish(self.discovery['percentage_state_topic'], self.speed)

        def set_speed(self, speed):
            self.command_queue.append(self.Command('speed', speed))
            self.apply_command()

        def apply_command(self, args=None, kwargs=None):
            if self.apply_mutex.acquire(blocking=False):
                if len(self.command_queue) == 0:
                    self.apply_mutex.release()
                    return
                try:
                    if self.command_queue[0].command == 'state':
                        self.toggle_switch()
                        self.state = self.command_queue[0].data == 'ON'
                        self.command_queue.clear()
                        if self.state:
                            self.set_speed(1)
                    self.apply_speed()
                finally:
                    self.apply_mutex.release()

        def apply_speed(self):
            if self.speed is None:
                return
            if self.power_updated is None:
                self.app.log('No power data yet')
                # self.apply_speed_timer.start()
                return
            if len(self.command_queue) == 0:
                return
            if self.has_power_jitter():
                # self.apply_speed_timer.start()
                return

            target_speed = self.command_queue[0].data
            if target_speed == 0:
                self.toggle_switch()
                self.command_queue.popleft()
                return
            diff = target_speed - self.speed
            if diff == 0:
                self.command_queue.popleft()
                return

            if self.apply_speed_sent is not None:
                if self.power_updated < self.apply_speed_sent:
                    self.app.log("Power data is older than last sent command")
                    # self.apply_speed_timer.start()
                    return
                if (self.power_updated - self.apply_speed_sent).seconds < 2:
                    self.app.log("Power data is too new")
                    # self.apply_speed_timer.start()
                    return
            if diff > 0:
                self.app.call_service("remote/send_command", entity_id=self.fanConfig.remote,
                                      command=self.fanConfig.up_command,
                                      num_repeats=diff, delay_secs=0.5)
            elif diff < 0:
                pass
                self.app.call_service("remote/send_command", entity_id=self.fanConfig.remote,
                                      command=self.fanConfig.down_command,
                                      num_repeats=-diff, delay_secs=0.5)
            self.apply_speed_sent = datetime.datetime.now().astimezone(pytz.UTC)
            # self.apply_speed_timer.start()

        def toggle_switch(self):
            self.app.call_service("switch/turn_off", entity_id=self.fanConfig.power_switch)
            time.sleep(1)
            self.app.call_service("switch/turn_on", entity_id=self.fanConfig.power_switch)
            time.sleep(1)

        def set_state(self, state):
            with self.apply_mutex:
                self.command_queue.insert(0, self.Command('state', state))

        def mqtt_event(self, event_name, data, kwargs):
            topic = data['topic']
            if topic == self.discovery['percentage_command_topic']:
                self.app.log(f"Setting speed {data['payload']}")
                with self.apply_mutex:
                    self.set_speed(int(data['payload']))
                self.publish_state()
            elif topic == self.discovery['command_topic']:
                self.app.log(f'Setting state {data["payload"]}')
                self.set_state(data['payload'])
                self.publish_state()

        @property
        def discovery(self):
            return {
                "device_class": "fan",
                "enabled_by_default": True,
                "object_id": f"{self.app.name}_{self.name}_fan",
                "unique_id": f"{self.app.name}_{self.name}_fan",
                "state_topic": f"{self.app.config.app_topic}/{self.name}_fan/state/state",
                "command_topic": f"{self.app.config.app_topic}/{self.name}_fan/state/set",
                "supported_features": 1,
                "name": None,
                "percentage_command_topic": f"{self.app.config.app_topic}/{self.name}_fan/percentage/set",
                "percentage_state_topic": f"{self.app.config.app_topic}/{self.name}_fan/percentage/state",
                "speed_count": len(self.state_ranges),
                "speed_range_min": 1,
                "speed_range_max": 3,
                "device": {
                    "identifiers": [
                        f"{self.app.name}_{self.name}"
                    ],
                    "manufacturer": "AlexMK",
                    "model": "HA IR Fan",
                    "name": f"{self.app.name}_{self.name}_fan_name"
                }}

    def __init__(self, ad: AppDaemon, name, logging, args, config, app_config, global_vars):
        super().__init__(ad, name, logging, args, config, app_config, global_vars)
        self.mq: mqttapi.Mqtt = None
        self.fans = {}

    def initialize(self):
        self.config = config.model_validate(self.app_config['ha_ir_fan']['config'])
        self.mq = self.get_plugin_api('MQTT')
        self.mq.mqtt_subscribe(self.config.app_topic + '/set', namespace='mqtt')

        for x in self.config.fans.keys():
            self.fans[x] = ha_ir_fan.irFanItem(x, self.config, self.config.fans[x], self)
