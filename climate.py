import asyncio
import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature, 
    HVACMode, 
    HVACAction,
    FAN_AUTO, 
    FAN_LOW, 
    FAN_MEDIUM, 
    FAN_HIGH
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify

def _climag_device_info(entry_id: str, master_name: str) -> DeviceInfo:
    """DeviceInfo condiviso fra tutte le entità della stessa config entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=master_name,
        manufacturer="ClimaG",
        model="Climate Controller",
        sw_version="1.2.0",
    )

_LOGGER = logging.getLogger(__name__)

DOMAIN = "climag"

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Configura l'entità principale ClimaG Master partendo dai dati salvati."""
    data = config_entry.data
    async_add_entities([
        ClimagClimate(
            hass=hass,
            config_entry=config_entry,
            name=data["name"],
            outdoor_temp_sensor=data.get("outdoor_temp_sensor"), 
            target_climates=data["target_climates"],
            heat_pump_entity=data["heat_pump_entity"],
            entry_id=config_entry.entry_id
        )
    ])

class ClimagClimate(ClimateEntity):
    """Il termostato master che coordina valvole, termostati e pompa di calore."""

    def __init__(self, hass, config_entry, name, outdoor_temp_sensor, target_climates, heat_pump_entity, entry_id):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = name
        self._outdoor_temp_sensor = outdoor_temp_sensor
        self._target_climates = target_climates
        self._heat_pump_entity = heat_pump_entity
        self._attr_unique_id = f"climag_{entry_id}"
        self._attr_device_info = _climag_device_info(entry_id, name)
        self._slug = slugify(name)
        
        self._select_entity_id = f"select.{self._slug}_climag_mode"
        
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE 
            | ClimateEntityFeature.TURN_ON 
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.FAN_MODE
        )
        
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
        self._attr_fan_mode = FAN_AUTO
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        
        self._attr_min_temp = 18.0
        self._attr_max_temp = 30.0
        self._attr_target_temperature_step = 0.5
        self._attr_target_temperature = 20.0
        
        self._valve_on_task = None
        self._termo_off_task = None
        self._mode_change_task = None
        self._master_cmd_task = None
        self._pump_command_lock = asyncio.Lock()
        self._pump_temp_task = None
        self._force_off_task = None
        # Task che spegne la pompa quando tutte le valvole restano chiuse oltre valve_off_delay (minuti)
        self._valve_off_task = None
        
        # Dizionario per tenere traccia dei task temporizzati attivi sulle singole zone
        self._zone_tasks = {}
        self._zone_startup_grace = {}
        
        self._last_sent_tmf = None
        self._last_sent_tmc = None

        self._lock_count = 0
        
        # VARIABILE RICHIESTA: Vera per 1 secondo solo quando hvac cambia dall'interfaccia Master climate
        self._master_cmd = False

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        
        # Tracciamento dei termostati
        self.async_on_remove(
            async_track_state_change_event(self.hass, self._target_climates, self._handle_thermostat_change)
        )
        
        valve_entities = [climate_id.replace("climate.", "binary_sensor.") + "_valve" for climate_id in self._target_climates]
        self.async_on_remove(
            async_track_state_change_event(self.hass, valve_entities, self._handle_valve_change)
        )
        
        _LOGGER.info("ClimaG Master monitora il selettore associato: %s", self._select_entity_id)
        self.async_on_remove(
            async_track_state_change_event(self.hass, self._select_entity_id, self._handle_select_change)
        )

    def _get_number_value(self, key: str, default: float) -> float:
        entity_id = f"number.{self._slug}_{key}"
        state = self.hass.states.get(entity_id)
        if state and state.state not in ["unknown", "unavailable"]:
            try:
                return float(state.state)
            except (TypeError, ValueError):
                _LOGGER.warning("Valore non valido per %s: %s. Uso default %s", entity_id, state.state, default)
        return default

    @property
    def ClimagMode(self) -> str:
        state = self.hass.states.get(self._select_entity_id)
        if state and state.state not in ["unknown", "unavailable"]:
            return state.state
        return "off"

    @property
    def Valve(self) -> bool:
        for climate_id in self._target_climates:
            valve_id = climate_id.replace("climate.", "binary_sensor.") + "_valve"
            state = self.hass.states.get(valve_id)
            if state and state.state == "on":
                return True
        return False

    def _active_zone_count(self) -> int:
        active_zones = 0
        for climate_id in self._target_climates:
            state = self.hass.states.get(climate_id)
            if state and state.state in ["cool", "heat"]:
                active_zones += 1
        return active_zones

    def _active_zone_modes(self) -> list[str]:
        modes = []
        for climate_id in self._target_climates:
            state = self.hass.states.get(climate_id)
            if state and state.state in ["cool", "heat"] and state.state not in modes:
                modes.append(state.state)
        return modes

    def _desired_pump_mode(self) -> HVACMode | None:
        active_modes = self._active_zone_modes()
        if self.ClimagMode in ["cool", "heat"]:
            return HVACMode(self.ClimagMode)
        if self._attr_hvac_mode in [HVACMode.COOL, HVACMode.HEAT]:
            return self._attr_hvac_mode
        if len(active_modes) == 1:
            return HVACMode(active_modes[0])
        return None

    def _schedule_valve_on_delay(self) -> None:
        if self._valve_on_task and self._valve_on_task.done():
            self._valve_on_task = None
        if self._valve_on_task:
            return
        if self._termo_off_task:
            self._termo_off_task.cancel()
            self._termo_off_task = None
        self._valve_on_task = asyncio.create_task(self._handle_valve_on_delay())

    def _mark_zone_startup_grace(self, entity_id: str) -> None:
        self._zone_startup_grace[entity_id] = self.hass.loop.time() + 5.0

    def _zone_is_in_startup_grace(self, entity_id: str) -> bool:
        expires_at = self._zone_startup_grace.get(entity_id)
        if expires_at is None:
            return False
        if self.hass.loop.time() <= expires_at:
            return True
        self._zone_startup_grace.pop(entity_id, None)
        return False

    def _pump_target_temperature(self, mode: HVACMode) -> float:
        return self.Tmf if mode == HVACMode.COOL else self.Tmc

    async def _async_wait_for_pump_mode(self, mode: HVACMode, timeout: float = 10.0) -> bool:
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            hp_state = self.hass.states.get(self._heat_pump_entity)
            if hp_state and hp_state.state == mode.value:
                return True
            await asyncio.sleep(0.5)
        hp_state = self.hass.states.get(self._heat_pump_entity)
        return bool(hp_state and hp_state.state == mode.value)

    async def _async_send_pump_temperature(self, mode: HVACMode) -> None:
        target_temp = self._pump_target_temperature(mode)
        if mode == HVACMode.COOL:
            if target_temp == self._last_sent_tmf:
                return
            _LOGGER.info("Invio temperatura mandata freddo alla pompa: %s°C", target_temp)
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._heat_pump_entity, "temperature": target_temp},
                blocking=True,
            )
            self._last_sent_tmf = target_temp
        elif mode == HVACMode.HEAT:
            if target_temp == self._last_sent_tmc:
                return
            _LOGGER.info("Invio temperatura mandata caldo alla pompa: %s°C", target_temp)
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._heat_pump_entity, "temperature": target_temp},
                blocking=True,
            )
            self._last_sent_tmc = target_temp

    @property
    def hvac_action(self) -> HVACAction:
        hp_state = self.hass.states.get(self._heat_pump_entity)
        if not hp_state or hp_state.state == "off":
            return HVACAction.OFF
        if hp_state.state == "heat":
            return HVACAction.HEATING
        if hp_state.state == "cool":
            return HVACAction.COOLING
        if hp_state.state == "fan_only":
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self):
        return {
            "valve": self.Valve,
            "calculated_tmf": self.Tmf,
            "calculated_tmc": self.Tmc,
            "max_delta_t": self.max_delta_t,
            "climag_mode_current": self.ClimagMode,
            "lock_active": self._lock_count > 0,
            "master_cmd": self._master_cmd
        }

    @property
    def current_temperature(self) -> float:
        temperatures = []
        for climate_id in self._target_climates:
            state = self.hass.states.get(climate_id)
            if state:
                cur_temp = state.attributes.get("current_temperature")
                if cur_temp is not None:
                    try:
                        temperatures.append(float(cur_temp))
                    except (ValueError, TypeError):
                        continue
        if temperatures:
            return round(sum(temperatures) / len(temperatures), 1)
        return None

    @property
    def target_temperature(self) -> float:
        return self._attr_target_temperature

    async def async_set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._attr_target_temperature = temperature
            self.async_write_ha_state()
            for climate_id in self._target_climates:
                await self.hass.services.async_call("climate", "set_temperature", {"entity_id": climate_id, "temperature": temperature})

    async def async_turn_on(self) -> None:
        """Accende il master nell'ultima modalita' utile, o in heat come fallback."""
        target_mode = self.ClimagMode if self.ClimagMode in ["heat", "cool", "fan_only"] else HVACMode.HEAT.value
        await self.async_set_hvac_mode(HVACMode(target_mode))

    async def async_turn_off(self) -> None:
        """Spegne il master usando la stessa logica del cambio modalita'."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()
        for climate_id in self._target_climates:
            try:
                await self.hass.services.async_call("climate", "set_fan_mode", {"entity_id": climate_id, "fan_mode": fan_mode})
            except Exception as err:
                _LOGGER.warning("Impossibile impostare fan_mode su %s: %s", climate_id, err)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Intercetta i comandi diretti dall'interfaccia Master (climate.climag_master)."""
        _LOGGER.info("Interfaccia ClimaG Master: cambio modalità richiesto su %s. Attivazione master_cmd.", hvac_mode)
        
        # Attivazione della variabile master_cmd e gestione del timer di 1 secondo
        self._master_cmd = True
        if self._master_cmd_task:
            self._master_cmd_task.cancel()
        self._master_cmd_task = asyncio.create_task(self._reset_master_cmd_after_delay())

        # Esecuzione del listener interno associato all'attivazione del comando
        self._handle_master_cmd_trigger(hvac_mode)

        # Sincronizza il selettore che piloterà a cascata il resto del sistema
        await self.hass.services.async_call(
            "select", 
            "select_option", 
            {"entity_id": self._select_entity_id, "option": str(hvac_mode.value)}
        )
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def _reset_master_cmd_after_delay(self):
        """Task asincrono che mantiene vera la variabile master_cmd per esattamente un secondo."""
        await asyncio.sleep(1.0)
        self._master_cmd = False
        self.async_write_ha_state()
        _LOGGER.debug("master_cmd resettato a False dopo 1 secondo.")

    def _handle_master_cmd_trigger(self, target_mode: HVACMode):
        """Listener interno: se l'azione arriva dal Master e la modalità è attiva (HEAT o COOL),

        comanda tutti i termostati spenti o in ventilazione ad accendersi in modo coerente.
        """
        if str(target_mode.value) not in ["cool", "heat"]:
            return

        _LOGGER.info("Listener master_cmd: Rilevato comando da entità climate Master. Allineo i termostati in off/fan_only su %s", target_mode.value)
        for climate_id in self._target_climates:
            z_state = self.hass.states.get(climate_id)
            if z_state and z_state.state in ["off", "fan_only"]:
                _LOGGER.info("master_cmd forza accensione per zona indipendente: %s -> %s", climate_id, target_mode.value)
                self.hass.async_create_task(
                    self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": str(target_mode.value)})
                )

    @property
    def dTf(self) -> float:
        """Calcola il massimo delta termico in modulo considerando solo i termostati in COOL o HEAT."""
        max_delta = 0.0
        for climate_id in self._target_climates:
            state = self.hass.states.get(climate_id)
            if not state or state.state not in ["cool", "heat"]:
                continue
            current_temp = state.attributes.get("current_temperature")
            target_temp = state.attributes.get("temperature")
            if current_temp is not None and target_temp is not None:
                delta = abs(float(current_temp) - float(target_temp))
                if delta > max_delta:
                    max_delta = delta
        return max_delta

    @property
    def max_delta_t(self) -> float:
        """Ritorna il delta T massimo reale (con segno: attuale - desiderata) tra i soli termostati in COOL o HEAT."""
        selected_delta = None
        for climate_id in self._target_climates:
            state = self.hass.states.get(climate_id)
            if not state or state.state not in ["cool", "heat"]:
                continue
            current_temp = state.attributes.get("current_temperature")
            target_temp = state.attributes.get("temperature")
            if current_temp is not None and target_temp is not None:
                real_delta = float(current_temp) - float(target_temp)
                if selected_delta is None or abs(real_delta) > abs(selected_delta):
                    selected_delta = real_delta
        return round(selected_delta, 2) if selected_delta is not None else 0.0

    @property
    def Tmf(self) -> float:
        tmf_b = self._get_number_value("tmf_b", 18.0)
        kpc = self._get_number_value("kpc", 2.0)
        tmf_min = self._get_number_value("tmf_min", 9.0)
        tmf_max = self._get_number_value("tmf_max", 12.0)
        return round(max(tmf_min, min(tmf_b - (kpc * self.dTf), tmf_max)), 2)

    @property
    def Tmc(self) -> float:
        tmc_b = self._get_number_value("tmc_b", 45.0)
        kpf = self._get_number_value("kpf", 1.0)
        tmc_min = self._get_number_value("tmc_min", 37.0)
        tmc_max = self._get_number_value("tmc_max", 50.0)
        return round(max(tmc_min, min(tmc_b + (kpf * self.dTf), tmc_max)), 2)

    def _check_and_update_pump_temperature(self):
        """Invia le temperature calcolate alla pompa solo se si trova nella rispettiva modalità corretta."""
        if self._pump_temp_task and not self._pump_temp_task.done():
            return
        self._pump_temp_task = self.hass.async_create_task(self._async_update_pump_temperature_if_running())

    async def _async_update_pump_temperature_if_running(self) -> None:
        async with self._pump_command_lock:
            hp_state = self.hass.states.get(self._heat_pump_entity)
            if not hp_state or hp_state.state not in ["cool", "heat"]:
                _LOGGER.debug("Salto aggiornamento temperatura pompa: pompa non attiva (%s)", hp_state.state if hp_state else None)
                return
            await self._async_send_pump_temperature(HVACMode(hp_state.state))

    async def _async_start_pump_if_still_needed(self) -> None:
        """Accende la pompa solo se, al momento del comando, la richiesta e' ancora valida."""
        async with self._pump_command_lock:
            mode = self._desired_pump_mode()
            if not mode or not self.Valve or self._active_zone_count() == 0:
                _LOGGER.info(
                    "Accensione pompa annullata: mode=%s, climag_mode=%s, master_mode=%s, valve=%s, active_zones=%s, active_modes=%s",
                    mode,
                    self.ClimagMode,
                    self._attr_hvac_mode,
                    self.Valve,
                    self._active_zone_count(),
                    self._active_zone_modes(),
                )
                return

            _LOGGER.info("Accendo pompa di calore in %s. Setpoint mandata rinviato finche' la pompa risulta attiva.", mode.value)
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._heat_pump_entity, "hvac_mode": mode.value},
                blocking=True,
            )
            if not await self._async_wait_for_pump_mode(mode, timeout=10.0):
                _LOGGER.warning(
                    "Pompa non confermata in %s entro 10 secondi. Non invio setpoint mandata per evitare comandi fantasma.",
                    mode.value,
                )
                return
            await self._async_send_pump_temperature(mode)

    async def _async_stop_pump_if_still_needed(self) -> None:
        """Spegne la pompa solo se non e' riapparsa una richiesta durante il delay."""
        async with self._pump_command_lock:
            if self._active_zone_count() > 0:
                _LOGGER.info(
                    "Spegnimento pompa annullato: ci sono ancora zone attive. climag_mode=%s, master_mode=%s, valve=%s, active_zones=%s, active_modes=%s",
                    self.ClimagMode,
                    self._attr_hvac_mode,
                    self.Valve,
                    self._active_zone_count(),
                    self._active_zone_modes(),
                )
                return

            hp_state = self.hass.states.get(self._heat_pump_entity)
            if hp_state and hp_state.state != "off":
                _LOGGER.info("Ritardo scaduto. Spengo la pompa di calore (%s)", self._heat_pump_entity)
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self._heat_pump_entity, "hvac_mode": "off"},
                    blocking=True,
                )

    def _schedule_force_off_watchdog(self) -> None:
        if self._force_off_task and not self._force_off_task.done():
            self._force_off_task.cancel()
        self._force_off_task = asyncio.create_task(self._handle_force_off_watchdog())

    def _cancel_force_off_watchdog(self) -> None:
        if self._force_off_task and not self._force_off_task.done():
            self._force_off_task.cancel()
        self._force_off_task = None

    async def _handle_force_off_watchdog(self) -> None:
        try:
            await asyncio.sleep(30.0)
            if self.ClimagMode != "off" or self._attr_hvac_mode != HVACMode.OFF:
                _LOGGER.info(
                    "Watchdog spegnimento annullato: ClimaG non e' piu' off (select=%s, master=%s).",
                    self.ClimagMode,
                    self._attr_hvac_mode,
                )
                return

            async with self._pump_command_lock:
                hp_state = self.hass.states.get(self._heat_pump_entity)
                if hp_state and hp_state.state != "off":
                    _LOGGER.warning(
                        "Watchdog spegnimento: ClimaG e' off da oltre 30s. Spengo forzatamente la pompa %s.",
                        self._heat_pump_entity,
                    )
                    await self.hass.services.async_call(
                        "climate",
                        "set_hvac_mode",
                        {"entity_id": self._heat_pump_entity, "hvac_mode": "off"},
                        blocking=True,
                    )
        finally:
            self._force_off_task = None

    @callback
    def _handle_thermostat_change(self, event):
        """Gestisce le variazioni provenienti dalle singole stanze (stati e attributi)."""
        if self._lock_count > 0:
            return

        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        entity_id = event.data.get("entity_id")
        
        if not new_state:
            return

        old_mode = old_state.state if old_state else "off"
        room_mode = new_state.state
        current_climag_mode = self.ClimagMode

        old_target = old_state.attributes.get("temperature") if old_state else None
        new_target = new_state.attributes.get("temperature")
        old_current = old_state.attributes.get("current_temperature") if old_state else None
        new_current = new_state.attributes.get("current_temperature")

        is_thermal_change = (old_mode == room_mode) and ((old_target != new_target) or (old_current != new_current))

        # Se è cambiato un attributo termico, aggiorna calcoli e stato in modo reattivo immediato
        if is_thermal_change:
            self._check_and_update_pump_temperature()
            self.async_write_ha_state()
            return

        if old_mode == room_mode and not is_thermal_change:
            return

        # --- LOGICA DI CONTROLLO DELLE ZONE ---
        if entity_id in self._zone_tasks:
            self._zone_tasks[entity_id].cancel()
            del self._zone_tasks[entity_id]

        if room_mode in ["cool", "heat"]:
            if current_climag_mode != "off" and old_mode in ["off", "fan_only"]:
                self._mark_zone_startup_grace(entity_id)
            self._cancel_force_off_watchdog()

            if current_climag_mode == "off":
                _LOGGER.info("REGOLA 1: %s passa a %s con Master OFF. Accendo subito il selettore.", entity_id, room_mode)
                self._lock_count += 1
                try:
                    if self._termo_off_task:
                        self._termo_off_task.cancel()
                        self._termo_off_task = None
                    self.hass.async_create_task(
                        self.hass.services.async_call("select", "select_option", {"entity_id": self._select_entity_id, "option": room_mode})
                    )
                    self._attr_hvac_mode = HVACMode(room_mode)
                    self.async_write_ha_state()
                    if self.Valve:
                        self._schedule_valve_on_delay()
                finally:
                    self._lock_count -= 1
            
            elif current_climag_mode != "off" and room_mode != current_climag_mode:
                if self._termo_off_task:
                    self._termo_off_task.cancel()
                    self._termo_off_task = None
                if old_mode in ["off", "fan_only"]:
                    _LOGGER.info("Pianifico adeguamento di %s su %s (ClimaG Mode corrente) tra 2 secondi.", entity_id, current_climag_mode)
                    self._zone_tasks[entity_id] = asyncio.create_task(
                        self._handle_zone_delay_action(entity_id, "align", current_climag_mode)
                    )
                elif old_mode in ["cool", "heat"]:
                    if self._zone_is_in_startup_grace(entity_id):
                        _LOGGER.info(
                            "Ignoro cambio modalita' iniziale di %s durante startup grace. Riallineo su ClimaG Mode: %s",
                            entity_id,
                            current_climag_mode,
                        )
                        self._zone_tasks[entity_id] = asyncio.create_task(
                            self._handle_zone_delay_action(entity_id, "align", current_climag_mode)
                        )
                    else:
                        _LOGGER.info("Pianifico inversione globale di ClimaG Mode su %s causata da %s tra 2 secondi.", room_mode, entity_id)
                        self._zone_tasks[entity_id] = asyncio.create_task(
                            self._handle_zone_delay_action(entity_id, "invert", room_mode)
                        )
        elif room_mode in ["off", "fan_only"]:
            self._zone_startup_grace.pop(entity_id, None)

        self._check_and_update_pump_temperature()
        
        active_zones = self._active_zone_count()

        if active_zones == 0 and current_climag_mode != "off" and self._lock_count == 0:
            _LOGGER.info("Tutte le zone sono spente. Spengo il sistema ClimaG Master ed avvio timer spegnimento pompa.")
            self._lock_count += 1
            try:
                self.hass.async_create_task(
                    self.hass.services.async_call("select", "select_option", {"entity_id": self._select_entity_id, "option": "off"})
                )
                self._attr_hvac_mode = HVACMode.OFF
                self._schedule_force_off_watchdog()
                self.async_write_ha_state()
                
                # Innesca il timer quando lo spegnimento è causato dall'azzeramento delle zone operative
                if not self._termo_off_task:
                    if self._valve_on_task:
                        self._valve_on_task.cancel()
                        self._valve_on_task = None
                    self._termo_off_task = asyncio.create_task(self._handle_termo_off_delay())
            finally:
                self._lock_count -= 1
        else:
            self.async_write_ha_state()

    async def _handle_zone_delay_action(self, entity_id, action_type, target_mode):
        await asyncio.sleep(2.0)
        self._lock_count += 1
        try:
            if action_type == "align":
                _LOGGER.info("Scaduti 2s. Forzo l'allineamento di %s su ClimaG Mode: %s", entity_id, target_mode)
                await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": target_mode})
            elif action_type == "invert":
                current_room_state = self.hass.states.get(entity_id)
                if current_room_state and current_room_state.state == target_mode:
                    _LOGGER.info("Scaduti 2s. L'inversione su %s è confermata. Cambio ClimaG Mode generale su: %s", entity_id, target_mode)
                    await self.hass.services.async_call("select", "select_option", {"entity_id": self._select_entity_id, "option": target_mode})
                    self._attr_hvac_mode = HVACMode(target_mode)
                    self.async_write_ha_state()
        finally:
            self._lock_count -= 1
            if entity_id in self._zone_tasks:
                del self._zone_tasks[entity_id]

    @callback
    def _handle_select_change(self, event):
        if self._lock_count > 0:
            return
        new_state = event.data.get("new_state")
        if not new_state:
            return
        new_mode = new_state.state
        _LOGGER.info("Il selettore generale ClimaG Mode è cambiato in %s. Esecuzione differita di climag_mode_delay.", new_mode)
        if self._mode_change_task:
            self._mode_change_task.cancel()
        self._mode_change_task = asyncio.create_task(self._handle_climag_mode_delay(new_mode))

    async def _handle_climag_mode_delay(self, target_mode):
        delay = self._get_number_value("climag_mode_delay", 0.0)
        _LOGGER.info("Attendo climag_mode_delay di %s secondi prima di applicare la modalità globale %s.", delay, target_mode)
        await asyncio.sleep(delay)
        
        self._lock_count += 1
        try:
            try:
                self._attr_hvac_mode = HVACMode(target_mode)
                if self._attr_hvac_mode == HVACMode.OFF:
                    self._schedule_force_off_watchdog()
                else:
                    self._cancel_force_off_watchdog()
                self.async_write_ha_state()
            except ValueError:
                pass

            for climate_id in self._target_climates:
                z_state = self.hass.states.get(climate_id)
                if not z_state:
                    continue
                if target_mode == "off" and z_state.state != "off":
                    await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "off"})
                elif z_state.state in ["cool", "heat"] and z_state.state != target_mode:
                    await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": target_mode})

            hp_state = self.hass.states.get(self._heat_pump_entity)
            if hp_state and hp_state.state != "off" and target_mode in ["cool", "heat"]:
                await self._async_start_pump_if_still_needed()
            elif target_mode in ["cool", "heat"] and self.Valve:
                self._schedule_valve_on_delay()
            elif hp_state and hp_state.state != "off" and target_mode == "fan_only":
                async with self._pump_command_lock:
                    await self.hass.services.async_call(
                        "climate",
                        "set_hvac_mode",
                        {"entity_id": self._heat_pump_entity, "hvac_mode": target_mode},
                        blocking=True,
                    )
            
            # NUOVA LOGICA: Se il selettore passa ad off (o ci è andato a seguito del delay), avvia lo spegnimento ritardato della pompa
            elif target_mode == "off" and hp_state and hp_state.state != "off":
                if not self._termo_off_task:
                    if self._valve_on_task:
                        self._valve_on_task.cancel()
                        self._valve_on_task = None
                    _LOGGER.info("ClimaG Mode impostato su OFF. Pianifico lo spegnimento ritardato della pompa di calore.")
                    self._termo_off_task = asyncio.create_task(self._handle_termo_off_delay())
        finally:
            self._lock_count -= 1

    @callback
    def _handle_valve_change(self, event):
        self.async_write_ha_state()
        if self.Valve:
            # Valvole aperte: annulla l'eventuale watchdog di spegnimento per valvole chiuse
            if self._valve_off_task and not self._valve_off_task.done():
                _LOGGER.debug("Valvole nuovamente aperte: annullo il watchdog valve_off_delay.")
                self._valve_off_task.cancel()
            self._valve_off_task = None
            self._schedule_valve_on_delay()
        else:
            if self._valve_on_task:
                self._valve_on_task.cancel()
                self._valve_on_task = None
            # Tutte le valvole chiuse: avvia il watchdog indipendente di spegnimento pompa
            self._schedule_valve_off_delay()

    def _schedule_valve_off_delay(self) -> None:
        """Pianifica lo spegnimento forzato della pompa dopo valve_off_delay minuti di valvole chiuse."""
        if self._valve_off_task and not self._valve_off_task.done():
            return
        self._valve_off_task = asyncio.create_task(self._handle_valve_off_delay())

    async def _handle_valve_off_delay(self) -> None:
        """Attende valve_off_delay (minuti) e spegne la pompa se le valvole sono ancora tutte chiuse."""
        delay_min = self._get_number_value("valve_off_delay", 0.0)
        delay_sec = max(0.0, delay_min * 60.0)
        _LOGGER.info(
            "Tutte le valvole risultano chiuse. Avvio watchdog valve_off_delay: %s min (%s s).",
            delay_min, delay_sec,
        )
        try:
            await asyncio.sleep(delay_sec)
            # Ricontrollo lo stato delle valvole dopo l'attesa, fuori dal lock per non ritardare altri task
            if self.Valve:
                _LOGGER.info("Watchdog valve_off_delay: valvole riaperte durante l'attesa, nessuna azione.")
                return
            async with self._pump_command_lock:
                # Doppio controllo dentro il lock per evitare race con accensione concorrente
                if self.Valve:
                    _LOGGER.info("Watchdog valve_off_delay: valvole riaperte appena prima dello spegnimento, annullo.")
                    return
                hp_state = self.hass.states.get(self._heat_pump_entity)
                if not hp_state or hp_state.state == "off":
                    _LOGGER.debug("Watchdog valve_off_delay: pompa gia' off, nessuna azione.")
                    return
                _LOGGER.warning(
                    "Watchdog valve_off_delay scaduto (%s min) con valvole tutte chiuse. Spengo forzatamente la pompa %s a prescindere da termostati e Master.",
                    delay_min, self._heat_pump_entity,
                )
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self._heat_pump_entity, "hvac_mode": "off"},
                    blocking=True,
                )
                # Pulisco la cache dei setpoint cosi' al prossimo avvio la temperatura verra' reinviata
                self._last_sent_tmf = None
                self._last_sent_tmc = None
        except asyncio.CancelledError:
            _LOGGER.debug("Watchdog valve_off_delay annullato.")
            raise
        finally:
            self._valve_off_task = None

    async def _handle_valve_on_delay(self):
        delay = self._get_number_value("valve_on_delay", 0.0)
        try:
            await asyncio.sleep(delay)
            await self._async_start_pump_if_still_needed()
        finally:
            self._valve_on_task = None

    async def _handle_termo_off_delay(self):
        # Utilizza il valore configurato nel componente number per gestire il ritardo dello spegnimento
        delay = self._get_number_value("termo_off_delay", 0.0)
        _LOGGER.info("Attendo termo_off_delay di %s secondi prima di spegnere la pompa di calore.", delay)
        try:
            await asyncio.sleep(delay)
            await self._async_stop_pump_if_still_needed()
        finally:
            self._termo_off_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancella i timer pendenti quando Home Assistant scarica l'entita'."""
        tasks = [
            self._valve_on_task,
            self._termo_off_task,
            self._mode_change_task,
            self._master_cmd_task,
            self._pump_temp_task,
            self._force_off_task,
            self._valve_off_task,
            *self._zone_tasks.values(),
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        self._zone_tasks.clear()
