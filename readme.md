# IR Fan controller for Home Assistant

This is a MQTT-based plugin for the AppDaemon to control an InfraRed-controlled fans over the MQTT
It works in tandem with monitoring power relay (e.g. zigbee plug with power monitoring)  to determine current fan speed,
based on power consumption.

Configuration:

```appdaemon.yml```

```yaml
    MQTT:
      type: mqtt
      namespace: mqtt
      client_host: 'MQTT_HOST'
      client_user: 'MQTT_USER
     client_password: 'MQTT_PASSWORD'
      verbose: true
```

```app.yaml```

```yaml
ha_ir_fan:
  class: ha_ir_fan
  module: ha_ir_fan
  config:
    #Home Assistant MQTT discovery topic
    discovery_topic: "discovery"
    #app topic for the plugin
    app_topic: "ha_ir_fan"
    fans:
      #fan name
      kitchen_fan_3:
        # power jitter. Async AC motors power consumption will jitter when changing the speed (inrush current).
        # To solve this, plugin will ignore when min and max power ratio will be higher than this value (10%) 
        # during the 5 seconds and wait till consumption stabilizes. 
        power_jitter: 0.1
        # the power sensor attribute
        power_attribute: power
        # the power relay entity
        power_switch: switch.f1_kitchen_fan
        # the remote to control the fan
        remote: remote.f1_kitchen_ir
        up_command: "b64:sgw0AA0pDSoNKiURDSomESYRJhEmESYRJhEmEQ0qDSkmESYRDSoNKQ0qDSoNKQ0qJhENKg0AAaIAAAAA"
        down_command: "b64:shc0AAwqDSoNKiURDSolESURJRIlESURJRElEQ0qDColESURDSoMKgwqDColEQ0qDCoMKg0AAaEAAAAA"
        # power values for each speed. in example 0-9 watts is for poweroff, 10-115 for speed 1, 
        # 116-140 for speed 2, 141-1000 for speed 3
        power_values: [ 9,115,140,1000 ]
```