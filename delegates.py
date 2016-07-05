#!/usr/bin/python -u
# -*- coding: utf-8 -*-

import dbus
import functools
import gobject
import logging
import os
import sc_utils
import signal
import sys
import traceback

# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from ve_utils import exit_on_error


class SystemCalcDelegate(object):
	def set_sources(self, dbusmonitor, settings, dbusservice):
		self._dbusmonitor = dbusmonitor
		self._settings = settings
		self._dbusservice = dbusservice

	def get_input(self):
		'''In derived classes this function should return the list or D-Bus paths used as input. This will be
		used to populate self._dbusmonitor. Paths should be ordered by service name.
		Example:
		def get_input(self):
			return [
				('com.victronenergy.battery', ['/ProductId']),
				('com.victronenergy.solarcharger', ['/ProductId'])]
		'''
		return []

	def get_output(self):
		'''In derived classes this function should return the list or D-Bus paths used as input. This will be
		used to create the D-Bus items in the com.victronenergy.system service. You can include a gettext
		field which will be used to format the result of the GetText reply.
		Example:
		def get_output(self):
			return [('/Hub', {'gettext': '%s'}), ('/Dc/Battery/Current', {'gettext': '%s A'})]
		'''
		return []

	def get_settings(self):
		'''In derived classes this function should return all settings (from com.victronenergy.settings)
		that are used in this class. The return value will be used to populate self._settings.
		Note that if you add a setting here, it will be created (using AddSettings of the D-Bus), if you
		do not want that, add your setting to the list returned by get_input.
		List item format: (<alias>, <path>, <default value>, <min value>, <max value>)
		def get_settings(self):
			return [('writevebussoc', '/Settings/SystemSetup/WriteVebusSoc', 0, 0, 1)]
		'''
		return []

	def update_values(self, newvalues):
		pass

	def device_added(self, service, instance, do_service_change=True):
		pass

	def device_removed(self, service, instance):
		pass


class HubTypeSelect(SystemCalcDelegate):
	def __init__(self):
		pass

	def get_input(self):
		return [
			('com.victronenergy.vebus', ['/Hub4/AcPowerSetpoint', '/Hub1/ChargeVoltage', '/Mgmt/Connection'])]

	def get_output(self):
		return [('/Hub', {'gettext': '%s'})]

	def device_added(self, service, instance, do_service_change=True):
		pass

	def device_removed(self, service, instance):
		pass

	def update_values(self, newvalues):
		# The code below should be executed after PV inverter data has been updated, because we need the
		# PV inverter total power to update the consumption.
		hub = None
		vebus_path = newvalues.get('/VebusService')
		if self._dbusmonitor.get_value(vebus_path, '/Hub4/AcPowerSetpoint') != None:
			hub = 4
		elif self._dbusmonitor.get_value(vebus_path, '/Hub1/ChargeVoltage') != None or \
			newvalues.get('/Dc/Pv/Power') != None:
			hub = 1
		elif newvalues.get('/Ac/PvOnOutput/Total/Power') != None:
			hub = 2
		elif newvalues.get('/Ac/PvOnGrid/Total/Power') != None or \
			newvalues.get('/Ac/PvOnGenset/Total/Power') != None:
			hub = 3
		newvalues['/Hub'] = hub


class Hub1Bridge(SystemCalcDelegate):
	def __init__(self, service_supervisor):
		self._solarchargers = []
		self._timer = None
		self._service_supervisor = service_supervisor

	def get_input(self):
		return [
			('com.victronenergy.vebus',
				['/Hub1/ChargeVoltage', '/State']),
			('com.victronenergy.solarcharger',
				['/Link/NetworkMode', '/Link/ChargeVoltage', '/State', '/FirmwareVersion'])]

	def device_added(self, service, instance, do_service_change=True):
		service_type = service.split('.')[2]
		if service_type != 'solarcharger':
			return
		self._solarchargers.append(service)
		self._update_solarchargers()
		if self._timer == None:
			# Update the solar charger every 10 seconds, because it has to switch to HEX mode each time
			# we write a value to its D-Bus service. Writing too often may block text messages.
			self._timer = gobject.timeout_add(10000, exit_on_error, self._on_timer)

	def device_removed(self, service, instance):
		if service in self._solarchargers:
			self._solarchargers.remove(service)
			if len(self._solarchargers) == 0 and self._timer != None:
				gobject.source_remove(self._timer)
				self._timer = None

	def _on_timer(self):
		self._update_solarchargers()
		return True

	def _update_solarchargers(self):
		vebus_path = self._get_vebus_path()
		if vebus_path == None:
			return
		charge_voltage = self._dbusmonitor.get_value(vebus_path, '/Hub1/ChargeVoltage')
		if charge_voltage == None:
			return # This is not a Hub-1 system, or a VE.Can Hub-1 system
		state = self._dbusmonitor.get_value(vebus_path, '/State')
		for service in self._solarchargers:
			if self._service_supervisor.is_busy(service):
				logging.debug('Solarcharger being supervised: {}'.format(service))
				continue
			# We use /Link/NetworkMode to detect Hub-1 support in the solarcharger. Existence of this item
			# implies existence of the other /Link/* fields
			try:
				network_mode_item = self._dbusmonitor.get_item(service, '/Link/NetworkMode')
				if network_mode_item.get_value() != None:
					network_mode_item.set_value(dbus.Int32(5, variant_level=1)) # On & Hub-1
					charge_voltage_item = self._dbusmonitor.get_item(service, '/Link/ChargeVoltage')
					charge_voltage_item.set_value(dbus.Double(charge_voltage, variant_level=1))
					firmware_version = self._dbusmonitor.get_value(service, '/FirmwareVersion')
					if state != None and firmware_version is not None and (firmware_version & 0x0FFF) == 0x0117:
						state_item = self._dbusmonitor.get_item(service, '/State')
						state_item.set_value(dbus.Int32(state, variant_level=1))
			except dbus.exceptions.DBusException:
				pass

	def _update_charge_current(self, newvalues):
		# Not used right now, because vebus does not offer a path to write the total charge current
		vebus_path = self._get_vebus_path(newvalues)
		if vebus_path == None:
			return
		charge_current_item = self._dbusmonitor.get_item(vebus_path, '/Hub1/ChargeCurrent')
		if charge_current_item.get_value() == None:
			return
		total_charge_current = 0
		for service in self._solarchargers:
			charge_current = self._dbusmonitor.get_value(service, '/Dc/0/Current')
			if charge_current != None:
				total_charge_current += charge_current
		charge_current_item.set_value(dbus.Double(total_charge_current, variant_level=1))

	def _get_vebus_path(self, newvalues=None):
		if newvalues == None:
			if '/VebusService' not in self._dbusservice:
				return None
			return self._dbusservice['/VebusService']
		return newvalues.get('/VebusService')


class ServiceMapper(SystemCalcDelegate):
	def __init__(self):
		pass

	def device_added(self, service, instance, do_service_change=True):
		path = self._get_service_mapping_path(service, instance)
		if path in self._dbusservice:
			self._dbusservice[path] = service
		else:
			self._dbusservice.add_path(path, service)

	def device_removed(self, service, instance):
		path = self._get_service_mapping_path(service, instance)
		if path in self._dbusservice:
			del self._dbusservice[path]

	def _get_service_mapping_path(self, service, instance):
		sn = sc_utils.service_instance_name(service, instance).replace('.', '_').replace('/', '_')
		return '/ServiceMapping/%s' % sn


class VebusSocWriter(SystemCalcDelegate):
	def __init__(self):
		SystemCalcDelegate.__init__(self)
		gobject.idle_add(exit_on_error, lambda: not self._write_vebus_soc())
		gobject.timeout_add(10000, exit_on_error, self._write_vebus_soc)

	def get_input(self):
		return [('com.victronenergy.vebus', ['/Soc'])]

	def get_settings(self):
		return [('writevebussoc', '/Settings/SystemSetup/WriteVebusSoc', 0, 0, 1)]

	def _write_vebus_soc(self):
		write_vebus_soc = self._settings['writevebussoc']
		if not write_vebus_soc:
			return True
		vebus_service = self._dbusservice['/VebusService']
		if vebus_service == None:
			return True
		soc = self._dbusservice['/Dc/Battery/Soc']
		if soc == None:
			return True
		active_battery_service = self._dbusservice['/ActiveBatteryService']
		if active_battery_service == None or active_battery_service.startswith('com.victronenergy.vebus'):
			return True
		logging.debug("writing this soc to vebus: %d", soc)
		try:
			# Vebus service may go offline while we write this SoC
			self._dbusmonitor.get_item(vebus_service, '/Soc').set_value(dbus.Double(soc, variant_level=1))
		except dbus.exceptions.DBusException:
			pass
		return True


class RelayState(SystemCalcDelegate):
	def set_sources(self, dbusmonitor, settings, dbusservice):
		SystemCalcDelegate.set_sources(self, dbusmonitor, settings, dbusservice)
		relays = sc_utils.gpio_paths('/etc/venus/relays')
		if len(relays) == 0:
			logging.info('No relays found')
			return
		self._relays = {}
		i = 0
		for r in relays:
			path = os.path.join(r, 'value')
			dbus_path = '/Relay/{}/State'.format(i)
			self._relays[dbus_path] = path
			self._dbusservice.add_path(dbus_path, value=None, writeable=True,
				onchangecallback=self._on_relay_state_changed)
			i += 1
		logging.info('Relays found: {}'.format(', '.join(self._relays.values())))
		gobject.idle_add(exit_on_error, lambda: not self._update_relay_state())
		gobject.timeout_add(5000, exit_on_error, self._update_relay_state)

	def _update_relay_state(self):
		# @todo EV Do we still need this? Maybe only at startup?
		for dbus_path, file_path in self._relays.items():
			try:
				with open(file_path, 'rt') as r:
					state = int(r.read().strip())
					self._dbusservice[dbus_path] = state
			except (IOError, ValueError):
				traceback.print_exc()
		return True

	def _on_relay_state_changed(self, dbus_path, value):
		try:
			path = self._relays[dbus_path]
			with open(path, 'wt') as w:
				w.write('1'  if value == 1 else '0')
			return True
		except (IOError, ValueError):
			traceback.print_exc()
			return False


class BuzzerControl(SystemCalcDelegate):
	def set_sources(self, dbusmonitor, settings, dbusservice):
		SystemCalcDelegate.set_sources(self, dbusmonitor, settings, dbusservice)
		paths = sc_utils.gpio_paths('/etc/venus/buzzer')
		if len(paths) == 0:
			logging.info('No buzzer found')
			return
		self._path = os.path.join(paths[0], 'value')
		self._buzzer_on = False
		self._timer = None
		self._dbusservice.add_path('/Buzzer/State', value=0, writeable=True,
			onchangecallback=lambda p,v: exit_on_error(self._on_buzzer_state_changed, v))
		logging.info('Buzzer found: {}'.format(self._path))

	def _on_buzzer_state_changed(self, value):
		if value == 1:
			if self._timer == None:
				self._timer = gobject.timeout_add(500, exit_on_error, self._on_timer)
				self._set_buzzer(True)
		elif self._timer != None:
			gobject.source_remove(self._timer)
			self._timer = None
			self._set_buzzer(False)
		return True

	def _on_timer(self):
		self._set_buzzer(not self._buzzer_on)
		return True

	def _set_buzzer(self, on):
		try:
			with open(self._path, 'wt') as w:
				w.write('1' if on else '0')
			self._buzzer_on = on
			return True
		except (IOError, ValueError):
			traceback.print_exc()
			return False


class LgCircuitBreakerDetect(SystemCalcDelegate):
	def __init__(self):
		SystemCalcDelegate.__init__(self)
		self._lg_battery = None

	def set_sources(self, dbusmonitor, settings, dbusservice):
		SystemCalcDelegate.set_sources(self, dbusmonitor, settings, dbusservice)
		self._dbusservice.add_path('/Dc/Battery/Alarms/CircuitBreakerTripped', value=None)

	def device_added(self, service, instance, do_service_change=True):
		service_type = service.split('.')[2]
		if service_type == 'battery' and self._dbusmonitor.get_value(service, '/ProductId') == 0xB004:
			logging.info('LG battery service appeared: %s' % service)
			self._lg_battery = service
			self._lg_voltage_buffer = []
			self._dbusservice['/Dc/Battery/Alarms/CircuitBreakerTripped'] = 0

	def device_removed(self, service, instance):
		if service == self._lg_battery:
			logging.info('LG battery service disappeared: %s' % service)
			self._lg_battery = None
			self._lg_voltage_buffer = None
			self._dbusservice['/Dc/Battery/Alarms/CircuitBreakerTripped'] = None

	def update_values(self, newvalues):
		vebus_path = newvalues.get('/VebusService')
		if self._lg_battery is None or vebus_path is None:
			return
		battery_current = self._dbusmonitor.get_value(self._lg_battery, '/Dc/0/Current')
		if battery_current is None or abs(battery_current) > 0.01:
			if len(self._lg_voltage_buffer) > 0:
				logging.debug('LG voltage buffer reset')
				self._lg_voltage_buffer = []
			return
		vebus_voltage = self._dbusmonitor.get_value(vebus_path, '/Dc/0/Voltage')
		if vebus_voltage is None:
			return
		self._lg_voltage_buffer.append(float(vebus_voltage))
		if len(self._lg_voltage_buffer) > 40:
			self._lg_voltage_buffer = self._lg_voltage_buffer[-40:]
		elif len(self._lg_voltage_buffer) < 20:
			return
		min_voltage = min(self._lg_voltage_buffer)
		max_voltage = max(self._lg_voltage_buffer)
		battery_voltage = self._dbusmonitor.get_value(self._lg_battery, '/Dc/0/Voltage')
		logging.debug('LG battery current V=%s I=%s' % (battery_voltage, battery_current))
		if min_voltage < 0.9 * battery_voltage or max_voltage > 1.1 * battery_voltage:
			logging.error('LG shutdown detected V=%s I=%s %s' % (battery_voltage, battery_current, self._lg_voltage_buffer))
			item = self._dbusmonitor.get_item(vebus_path, '/Mode')
			if item is None:
				logging.error('Cannot switch off vebus device')
			else:
				self._dbusservice['/Dc/Battery/Alarms/CircuitBreakerTripped'] = 2
				item.set_value(dbus.Int32(4, variant_level=1))
				self._lg_voltage_buffer = []


class ServiceSupervisor(SystemCalcDelegate):
	def __init__(self):
		SystemCalcDelegate.__init__(self)
		self._supervised = set()
		self._busy = set()
		gobject.timeout_add(60000, exit_on_error, self._process_supervised)

	def get_input(self):
		return [
			('com.victronenergy.battery', ['/ProductId']),
			('com.victronenergy.solarcharger', ['/ProductId'])]

	def device_added(self, service, instance, do_service_change=True):
		service_type = service.split('.')[2]
		if service_type == 'battery' or service_type == 'solarcharger':
			self._supervised.add(service)

	def device_removed(self, service, instance):
		self._supervised.discard(service)
		self._busy.discard(service)

	def is_busy(self, service):
		return service in self._busy

	def _process_supervised(self):
		for service in self._supervised:
			# Do an async call. If the owner of the service does not answer, we do not want to wait for
			# the timeout here.
			# Do not use lambda function in the async call, because the lambda functions will be executed
			# after completion of the loop, and the service parameter will have the value that was assigned
			# to it in the last iteration. Instead we use functools.partial, which will 'freeze' the current
			# value of service.
			self._busy.add(service)
			self._dbusmonitor.dbusConn.call_async(
				service, '/ProductId', None, 'GetValue', '', [],
				functools.partial(exit_on_error, self._supervise_success, service),
				functools.partial(exit_on_error, self._supervise_failed, service))
		return True

	def _supervise_success(self, service, value):
		self._busy.discard(service)

	def _supervise_failed(self, service, error):
		try:
			self._busy.discard(service)
			if error.get_dbus_name() != 'org.freedesktop.DBus.Error.NoReply':
				logging.info('Ignoring supervise error from %s: %s' % (service, error))
				return
			logging.error('%s is not responding to D-Bus requests' % service)
			pid = self._dbusmonitor.dbusConn.call_blocking('org.freedesktop.DBus', '/', None,
				'GetConnectionUnixProcessID', 's', [service])
			if pid is not None and pid > 1:
				logging.error('killing owner of %s (pid=%s)' % (service, pid))
				os.kill(pid, signal.SIGKILL)
		except (OSError, dbus.exceptions.DBusException):
			traceback.print_exc()
