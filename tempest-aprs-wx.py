#!/usr/bin/env python3
"""
Tempest Weather Station Data Fetcher
Fetches current weather data from WeatherFlow Tempest API
"""

import requests
import json
from datetime import datetime
from typing import Dict, Optional, Tuple
import logging

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
            'wind_direction': int(wind_direction) if wind_direction is not None else None,
            'wind_speed_mph': round(ms_to_mph(wind_avg)) if wind_avg is not None else None,
            'wind_gust_mph': round(ms_to_mph(wind_gust)) if wind_gust is not None else None,
            'temperature_f': round(celsius_to_fahrenheit(temperature_c)) if temperature_c is not None else None,
            'temperature_c': temperature_c,
            'humidity_percent': int(humidity) if humidity is not None else None,
            'pressure_mb': pressure_mb,
            'pressure_inhg': round(pressure_mb * 0.02953, 2) if pressure_mb is not None else None,
            'solar_radiation': int(solar_radiation) if solar_radiation is not None else None,
            'rain_1hr_in': round(rain_1hr, 2) if rain_1hr is not None else None,
            'rain_1hr_mm': safe_get('precip_accum_last_1hr')
        }
        
        # Log the parsed data
        logger.info(f"Parsed weather data:")
        logger.info(f"  Temperature: {weather_data['temperature_f']}°F ({weather_data['temperature_c']}°C)")
        logger.info(f"  Humidity: {weather_data['humidity_percent']}%")
        logger.info(f"  Wind: {weather_data['wind_direction']}° at {weather_data['wind_speed_mph']} mph")
        if weather_data['wind_gust_mph']:
            logger.info(f"  Wind gust: {weather_data['wind_gust_mph']} mph")
        logger.info(f"  Pressure: {weather_data['pressure_mb']} mb ({weather_data['pressure_inhg']} inHg)")
        logger.info(f"  Solar radiation: {weather_data['solar_radiation']} W/m²")
        
        return weather_data
    
    def get_current_weather(self) -> Optional[Dict]:
        """Get current weather data (convenience method)"""
        observations = self.get_current_observations()
        if observations:
            return self.parse_weather_data(observations)
        return None


def main():
    """Example usage"""
    # Configuration - replace with your actual values
    STATION_ID = "150778"  # Replace with your station ID
    API_KEY = "fb61cf09-043c-4721-b5e9-2cd454b4e5ca"  # Replace with your API key
    
    if API_KEY == "your-api-key-here":
        print("Please set your actual API key and station ID in the script")
        return
    
    # Create weather station client
    station = TempestWeatherStation(STATION_ID, API_KEY)
    
    # Get station location
    location = station.get_station_location()
    if location:
        print(f"Station Location: {location[0]:.4f}, {location[1]:.4f}")
    
    # Get current weather
    weather = station.get_current_weather()
    if weather:
        print("\nCurrent Weather:")
        print(f"Temperature: {weather['temperature_f']}°F")
        print(f"Humidity: {weather['humidity_percent']}%")
        print(f"Wind: {weather['wind_direction']}° at {weather['wind_speed_mph']} mph")
        if weather['wind_gust_mph']:
            print(f"Wind Gust: {weather['wind_gust_mph']} mph")
        print(f"Pressure: {weather['pressure_mb']} mb")
        print(f"Solar Radiation: {weather['solar_radiation']} W/m²")
        
        # Print raw JSON for debugging
        print(f"\nRaw weather data:")
        print(json.dumps(weather, indent=2, default=str))
    else:
        print("Failed to get weather data")


if __name__ == "__main__":
    main()
