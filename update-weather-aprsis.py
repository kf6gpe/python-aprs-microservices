#!/usr/bin/env python3
"""
Tempest Weather Station to APRS-IS Bridge
Fetches weather data from Tempest and transmits to APRS-IS

Run this script periodically for weather updates. On Linux, put something like

*/10 * * * *  /home/kf6gpe/python-tempestwx-aprsis.py

in your crontab to run it every ten minutes.

(C) 2026 Ray Rischpater, KF6GPE.
This file provided under the MIT License.

"""

import requests
import json
import socket
import time
from datetime import datetime
from typing import Dict, Optional, Tuple
from pathlib import Path
import logging
import yaml

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TempestWeatherStation:
    """Client for fetching data from WeatherFlow Tempest API"""
    
    def __init__(self, station_id: str, api_key: str):
        self.station_id = station_id
        self.api_key = api_key
        self.base_url = "https://swd.weatherflow.com/swd/rest"
        
    def get_station_info(self) -> Optional[Dict]:
        """Get station information including location"""
        url = f"{self.base_url}/stations/{self.station_id}"
        params = {"token": self.api_key}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Successfully fetched station info for station {self.station_id}")
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch station info: {e}")
            return None
            
    def get_station_location(self) -> Optional[Tuple[float, float]]:
        """Get station latitude and longitude"""
        station_info = self.get_station_info()
        if not station_info or 'stations' not in station_info:
            return None
            
        stations = station_info['stations']
        if not stations:
            return None
            
        station = stations[0]  # Get first station
        latitude = station.get('latitude')
        longitude = station.get('longitude')
        
        if latitude is not None and longitude is not None:
            logger.info(f"Station location: {latitude:.4f}, {longitude:.4f}")
            return latitude, longitude
        
        return None
    
    def get_current_observations(self) -> Optional[Dict]:
        """Get current weather observations"""
        url = f"{self.base_url}/observations/station/{self.station_id}"
        params = {"token": self.api_key}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Successfully fetched observations for station {self.station_id}")
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch observations: {e}")
            return None
    
    def parse_weather_data(self, observations: Dict) -> Optional[Dict]:
        """Parse Tempest observations into standardized weather data"""
        if not observations or 'obs' not in observations:
            logger.error("No observations data found")
            return None
            
        obs_list = observations['obs']
        if not obs_list:
            logger.error("Empty observations list")
            return None
            
        # Get the most recent observation
        latest_obs = obs_list[0]
        
        # Extract weather values with safe access
        def safe_get(key, default=None):
            return latest_obs.get(key, default)
        
        # Unit conversions
        def celsius_to_fahrenheit(c):
            return (c * 9/5) + 32 if c is not None else None
            
        def ms_to_mph(ms):
            return ms * 2.237 if ms is not None else None
            
        def mm_to_inches_hundredths(mm):
            return mm * 3.937 if mm is not None else None
        
        # Parse values
        wind_avg = safe_get('wind_avg')
        wind_gust = safe_get('wind_gust') 
        wind_direction = safe_get('wind_direction')
        temperature_c = safe_get('air_temperature')
        humidity = safe_get('relative_humidity')
        pressure_mb = safe_get('barometric_pressure')
        solar_radiation = safe_get('solar_radiation')
        rain_1hr = safe_get('precip_accum_last_1hr')
        
        # Build weather data dictionary
        weather_data = {
            'timestamp': datetime.fromtimestamp(safe_get('timestamp', 0)),
            'wind_direction': int(wind_direction) if wind_direction is not None else 0,
            'wind_speed_mph': round(ms_to_mph(wind_avg)) if wind_avg is not None else 0,
            'wind_gust_mph': round(ms_to_mph(wind_gust)) if wind_gust is not None else None,
            'temperature_f': round(celsius_to_fahrenheit(temperature_c)) if temperature_c is not None else 0,
            'humidity_percent': int(humidity) if humidity is not None else 0,
            'pressure_mb': pressure_mb if pressure_mb is not None else 0,
            'solar_radiation': int(solar_radiation) if solar_radiation is not None else 0,
            'rain_1hr_hundredths': round(mm_to_inches_hundredths(rain_1hr)) if rain_1hr is not None else 0,
        }
        
        return weather_data
    
    def get_current_weather(self) -> Optional[Dict]:
        """Get current weather data (convenience method)"""
        observations = self.get_current_observations()
        if observations:
            return self.parse_weather_data(observations)
        return None


class APRSClient:
    """APRS-IS client for transmitting weather data"""
    
    def __init__(self, callsign: str, passcode: str):
        self.callsign = callsign.upper()
        self.passcode = passcode
        self.socket = None
        self.servers = [
            ("rotate.aprs.net", 14580),
            ("noam.aprs2.net", 14580),
            ("euro.aprs2.net", 14580),
            ("asia.aprs2.net", 14580)
        ]
    
    def connect(self, server_host: str = "rotate.aprs.net", server_port: int = 14580) -> bool:
        """Connect to APRS-IS server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)
            self.socket.connect((server_host, server_port))
            logger.info(f"Connected to {server_host}:{server_port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to {server_host}:{server_port}: {e}")
            return False
    
    def authenticate(self, software: str = "TempestAPRS", version: str = "1.0") -> bool:
        """Authenticate with APRS-IS server"""
        if not self.socket:
            logger.error("Not connected to server")
            return False
        
        login_string = f"user {self.callsign} pass {self.passcode} vers {software} {version}\r\n"
        
        try:
            self.socket.send(login_string.encode('ascii'))
            logger.info(f"Authenticating as {self.callsign}...")
            
            # Wait for server response
            time.sleep(1)
            response = self.socket.recv(1024).decode('ascii', errors='ignore')
            logger.info(f"Server response: {response.strip()}")
            
            if "verified" in response.lower():
                logger.info("Authentication successful!")
                return True
            else:
                logger.warning("Authentication may have failed, but continuing...")
                return True  # Some servers don't send clear verification
                
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    def format_weather_packet(self, callsign: str, latitude: float, longitude: float, 
                            weather_data: Dict, comment: str = "") -> str:
        """Format APRS weather packet"""
        
        # Convert coordinates to APRS format
        def format_latitude(lat):
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - lat_deg) * 60
            lat_dir = "N" if lat >= 0 else "S"
            return f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"
        
        def format_longitude(lon):
            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - lon_deg) * 60
            lon_dir = "E" if lon >= 0 else "W"
            return f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"
        
        lat_str = format_latitude(latitude)
        lon_str = format_longitude(longitude)
        
        # Extract weather values with defaults
        wind_dir = weather_data.get('wind_direction', 0)
        wind_speed = weather_data.get('wind_speed_mph', 0)
        wind_gust = weather_data.get('wind_gust_mph')
        temp = weather_data.get('temperature_f', 0)
        humidity = weather_data.get('humidity_percent', 0)
        pressure = weather_data.get('pressure_mb', 0)
        solar = weather_data.get('solar_radiation', 0)
        rain = weather_data.get('rain_1hr_hundredths', 0)
        
        # Format weather string
        weather_str = f"{wind_dir:03d}/{wind_speed:03d}"
        
        # Add wind gust if present
        if wind_gust is not None and wind_gust > 0:
            weather_str += f"g{wind_gust:03d}"
        
        # Add temperature, rain, humidity, pressure, luminosity
        weather_str += f"t{temp:03d}r{rain:03d}"
        
        # Format humidity (100% = 00, 0% = 01, others as-is)
        if humidity == 100:
            humidity_aprs = 0
        elif humidity == 0:
            humidity_aprs = 1
        else:
            humidity_aprs = min(99, max(1, humidity))
        
        weather_str += f"h{humidity_aprs:02d}"
        weather_str += f"b{int(pressure * 10):05d}"  # Convert to tenths of millibars
        weather_str += f"l{solar:04d}"
        
        # Create timestamp
        now = datetime.utcnow()
        timestamp = f"{now.day:02d}{now.hour:02d}{now.minute:02d}z"
        
        # Build complete packet
        packet = f"{callsign.upper()}>APRS:@{timestamp}{lat_str}/{lon_str}_{weather_str}{comment}"
        
        return packet
    
    def send_packet(self, packet: str) -> bool:
        """Send APRS packet to server"""
        if not self.socket:
            logger.error("Not connected to server")
            return False
        
        try:
            packet_with_newline = packet + "\r\n"
            self.socket.send(packet_with_newline.encode('ascii'))
            logger.info(f"Sent packet: {packet}")
            return True
        except Exception as e:
            logger.error(f"Failed to send packet: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from APRS-IS server"""
        if self.socket:
            try:
                self.socket.close()
                logger.info("Disconnected from APRS-IS")
            except:
                pass
            self.socket = None


class TempestAPRSBridge:
    """Bridge between Tempest weather station and APRS-IS"""
    
    def __init__(self, station_id: str, api_key: str, callsign: str, passcode: str):
        self.tempest = TempestWeatherStation(station_id, api_key)
        self.aprs = APRSClient(callsign, passcode)
        self.callsign = callsign
    
    def transmit_weather(self, comment: str = "Tempest WX") -> bool:
        """Fetch weather data and transmit to APRS-IS"""
        logger.info("Starting weather transmission...")
        
        # Get station location
        location = self.tempest.get_station_location()
        if not location:
            logger.error("Failed to get station location")
            return False
        
        latitude, longitude = location
        
        # Get current weather
        weather = self.tempest.get_current_weather()
        if not weather:
            logger.error("Failed to get weather data")
            return False
        
        # Display weather summary
        logger.info("Weather data retrieved:")
        logger.info(f"  Temperature: {weather['temperature_f']}°F")
        logger.info(f"  Humidity: {weather['humidity_percent']}%")
        logger.info(f"  Wind: {weather['wind_direction']}° at {weather['wind_speed_mph']} mph")
        if weather.get('wind_gust_mph'):
            logger.info(f"  Wind gust: {weather['wind_gust_mph']} mph")
        logger.info(f"  Pressure: {weather['pressure_mb']:.1f} mb")
        
        # Connect to APRS-IS
        if not self.aprs.connect():
            logger.error("Failed to connect to APRS-IS")
            return False
        
        try:
            # Authenticate
            if not self.aprs.authenticate():
                logger.error("Failed to authenticate with APRS-IS")
                return False
            
            # Format and send packet
            packet = self.aprs.format_weather_packet(
                self.callsign, latitude, longitude, weather, comment
            )
            
            if self.aprs.send_packet(packet):
                logger.info("Weather packet transmitted successfully!")
                return True
            else:
                logger.error("Failed to send weather packet")
                return False
                
        finally:
            self.aprs.disconnect()


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file"""
    if config_path is None:
        # Default to config.yaml in the same directory as the script
        config_path = Path(__file__).parent / "config.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Validate required fields
    required_fields = ['WXCallSign', 'Passcode', 'TempestStationID', 'TempestAPIKey']
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required config field: {field}")

    return config


def main():
    """Main entry point"""
    # Load configuration from YAML file
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        return

    # Extract configuration values
    station_id = config['TempestStationID']
    api_key = config['TempestAPIKey']
    callsign = config['WXCallSign']
    passcode = config['Passcode']

    logger.info("Tempest Weather Station to APRS-IS Bridge")
    logger.info(f"Station ID: {station_id}")
    logger.info(f"Callsign: {callsign}")

    # Create bridge and transmit weather
    bridge = TempestAPRSBridge(station_id, api_key, callsign, passcode)

    success = bridge.transmit_weather(comment="Tempest Weather Station")

    if success:
        print("Weather data transmitted to APRS-IS successfully!")
        print("Check aprs.fi for your callsign to see the weather report.")
    else:
        print("Failed to transmit weather data")


if __name__ == "__main__":
    main()
