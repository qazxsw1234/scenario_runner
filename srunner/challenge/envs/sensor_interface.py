import copy
import logging
import numpy as np
import os
import time
from threading import Thread

import carla


def threaded(fn):
    def wrapper(*args, **kwargs):
        thread = Thread(target=fn, args=args, kwargs=kwargs)
        thread.setDaemon(True)
        thread.start()

        return thread
    return wrapper

class HDMapMeasurement(object):
    def __init__(self, data, frame_number):
        self.data = data
        self.frame_number = frame_number


class HDMapReader(object):
    def __init__(self, vehicle, reading_frequency=1.0):
        self._vehicle = vehicle
        self._reading_frequency = reading_frequency
        self._CARLA_ROOT = os.getenv('CARLA_ROOT', "./")
        self._callback = None
        self._frame_number = 0
        self._run_ps = True
        self.run()

    def __call__(self):
        map_name = os.path.basename(self._vehicle.get_world().get_map().name)
        transform = self._vehicle.get_transform()

        return {'map_file': "{}/HDMaps/{}.ply".format(self._CARLA_ROOT, map_name),
                'transform': {'x': transform.location.x,
                              'y': transform.location.y,
                              'z': transform.location.z,
                              'yaw': transform.rotation.yaw,
                              'pitch': transform.rotation.pitch,
                              'roll': transform.rotation.roll}
                }

    @threaded
    def run(self):
        latest_read = time.time()
        while self._run_ps:
            if self._callback is not None:
                capture = time.time()
                if capture - latest_read > (1 / self._reading_frequency):
                    self._callback(HDMapMeasurement(self.__call__(), self._frame_number))
                    self._frame_number += 1
                    latest_read = time.time()
                else:
                    time.sleep(0.001)

    def listen(self, callback):
        # Tell that this function receives what the producer does.
        self._callback = callback

    def destroy(self):
        self._run_ps = False


class CANBusMeasurement(object):
    def __init__(self, data, frame_number):
        self.data = data
        self.frame_number = frame_number


class CANBusSensor(object):
    """
    CAN BUS pseudo sensor that gets to read all the vehicle proprieties including speed.
    This sensor is not placed at the CARLA environment. It is
    only an asynchronous interface to the forward speed.
    """

    def __init__(self, vehicle, reading_frequency):
        # The vehicle where the class reads the speed
        self._vehicle = vehicle
        # How often do you look at your speedometer in hz
        self._reading_frequency = reading_frequency
        self._callback = None
        #  Counts the frames
        self._frame_number = 0
        self._run_ps = True
        self.read_CAN_Bus()

    def _get_forward_speed(self):
        """ Convert the vehicle transform directly to forward speed """

        velocity = self._vehicle.get_velocity()
        transform = self._vehicle.get_transform()
        vel_np = np.array([velocity.x, velocity.y, velocity.z])
        pitch = np.deg2rad(transform.rotation.pitch)
        yaw = np.deg2rad(transform.rotation.yaw)
        orientation = np.array([np.cos(pitch) * np.cos(yaw), np.cos(pitch) * np.sin(yaw), np.sin(pitch)])
        speed = np.dot(vel_np, orientation)
        return speed

    def __call__(self):

        """ We convert the vehicle physics information into a convenient dictionary """

        vehicle_physics = self._vehicle.get_physics_control()
        wheels_list_dict = []
        for wheel in vehicle_physics.wheels:
            wheels_list_dict.append(
                {'tire_friction': wheel.tire_friction,
                 'damping_rate': wheel.damping_rate,
                 'steer_angle': wheel.steer_angle,
                 'disable_steering': wheel.disable_steering

                 }
            )

        torque_curve = []
        for point in vehicle_physics.torque_curve:
            torque_curve.append({'x': point.x,
                                'y': point.y
                                })
        steering_curve = []
        for point in vehicle_physics.steering_curve:
            steering_curve.append({'x': point.x,
                                'y': point.y
                                })

        return {
            'speed': self._get_forward_speed(),
            'torque_curve': torque_curve,
            'max_rpm': vehicle_physics.max_rpm,
            'moi': vehicle_physics.moi,
            'damping_rate_full_throttle': vehicle_physics.damping_rate_full_throttle,
            'damping_rate_zero_throttle_clutch_disengaged':
                vehicle_physics.damping_rate_zero_throttle_clutch_disengaged,
            'use_gear_autobox': vehicle_physics.use_gear_autobox,
            'clutch_strength': vehicle_physics.clutch_strength,
            'mass': vehicle_physics.mass,
            'drag_coefficient': vehicle_physics.drag_coefficient,
            'center_of_mass': {'x': vehicle_physics.center_of_mass.x,
                               'y': vehicle_physics.center_of_mass.x,
                               'z': vehicle_physics.center_of_mass.x
                               },
            'steering_curve': steering_curve,
            'wheels': wheels_list_dict
        }




    @threaded
    def read_CAN_Bus(self):
        latest_speed_read = time.time()
        while self._run_ps:
            if self._callback is not None:
                capture = time.time()
                if capture - latest_speed_read > (1 / self._reading_frequency):
                    self._callback(CANBusMeasurement(self.__call__(), self._frame_number))
                    self._frame_number += 1
                    latest_speed_read = time.time()
                else:
                    time.sleep(0.001)

    def listen(self, callback):
        # Tell that this function receives what the producer does.
        self._callback = callback

    def destroy(self):
        self._run_ps = False


class CallBack(object):
    def __init__(self, tag, sensor, data_provider):
        self._tag = tag
        self._data_provider = data_provider

        self._data_provider.register_sensor(tag, sensor)

    def __call__(self, data):
        if isinstance(data, carla.Image):
            self._parse_image_cb(data, self._tag)
        elif isinstance(data, carla.LidarMeasurement):
            self._parse_lidar_cb(data, self._tag)
        elif isinstance(data, carla.GnssEvent):
            self._parse_gnss_cb(data, self._tag)
        elif isinstance(data, CANBusMeasurement):
            self._parse_speedometer(data, self._tag)
        elif isinstance(data, HDMapMeasurement):
            self._parse_hdmap(data, self._tag)
        else:
            logging.error('No callback method for this sensor.')

    def _parse_image_cb(self, image, tag):
        array = np.frombuffer(image.raw_data, dtype=np.dtype("uint8"))
        array = copy.deepcopy(array)
        array = np.reshape(array, (image.height, image.width, 4))
        array = array[:, :, :3]
        array = array[:, :, ::-1]
        self._data_provider.update_sensor(tag, array, image.frame_number)

    def _parse_lidar_cb(self, lidar_data, tag):
        points = np.frombuffer(lidar_data.raw_data, dtype=np.dtype('f4'))
        points = copy.deepcopy(points)
        points = np.reshape(points, (int(points.shape[0] / 3), 3))
        self._data_provider.update_sensor(tag, points, lidar_data.frame_number)

    def _parse_gnss_cb(self, gnss_data, tag):
        array = np.array([gnss_data.latitude,
                          gnss_data.longitude,
                          gnss_data.altitude], dtype=np.float32)
        self._data_provider.update_sensor(tag, array, gnss_data.frame_number)

    def _parse_speedometer(self, speed, tag):
        self._data_provider.update_sensor(tag, speed.data, speed.frame_number)

    def _parse_hdmap(self, hd_package, tag):
        self._data_provider.update_sensor(tag, hd_package.data, hd_package.frame_number)


class SensorInterface(object):
    def __init__(self):
        self._sensors_objects = {}
        self._data_buffers = {}
        self._timestamps = {}

    def register_sensor(self, tag, sensor):
        if tag  in self._sensors_objects:
            raise ValueError("Duplicated sensor tag [{}]".format(tag))

        self._sensors_objects[tag] = sensor
        self._data_buffers[tag] = None
        self._timestamps[tag] = -1

    def update_sensor(self, tag, data, timestamp):
        if tag not in self._sensors_objects:
            raise ValueError("The sensor with tag [{}] has not been created!".format(tag))
        self._data_buffers[tag] = data
        self._timestamps[tag] = timestamp

    def all_sensors_ready(self):
        for key in self._sensors_objects.keys():
            if self._data_buffers[key] is None:
                return False
        return True

    def get_data(self):
        data_dict = {}

        for key in self._sensors_objects.keys():
            data_dict[key] = (self._timestamps[key], copy.deepcopy(self._data_buffers[key]))
        return data_dict