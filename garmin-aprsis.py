#!/usr/bin/env python3
"""
Garmin Explore KML to APRS-IS one-shot bridge

Fetches the most recent position from a Garmin Explore MapShare KML feed and
gates it to APRS-IS if it is newer than what has already been reported for the
transmit callsign. Unlike garmin-aprsis-bridge.py, this does its work once and
then exits, making it suitable for periodic invocation from cron.

"Newer" is decided against two different clocks, because they are not
comparable:

  * Against our own previous run, using the Garmin fix timestamp recorded in a
    small state file next to this script. aprs.fi reports when a packet was
    *heard*, not the fix time carried in its payload, so comparing the KML fix
    time against aprs.fi's timestamp for a packet we ourselves gated always
    looks "older" and silently drops fixes.
  * Against a genuinely different source for the same callsign (a real radio
    beaconing KF6GPE-9, say), looked up on aprs.fi. If something else has
    reported more recently, we leave it alone rather than clobbering a live
    position with a laggier Garmin fix.

The last-known APRS position is looked up via the aprs.fi API, which requires a
free API key (see https://aprs.fi/page/api). Put it in config.yaml as
APRSFIApiKey.

For example, to run every five minutes, put something like this in your crontab:

*/5 * * * * (cd /home/kf6gpe ; /home/kf6gpe/garmin-aprsis.py >> /home/kf6gpe/garmin-aprsis.log 2>&1)

(C) 2026 Ray Rischpater, KF6GPE.
This file provided under the MIT License.
"""

import json
import os
import requests
import socket
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional, Dict
from dataclasses import dataclass
from pathlib import Path
import logging
import re
import yaml

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Comment attached to every packet we gate. Also how we recognize our own
# packets when they come back to us from aprs.fi.
GATE_COMMENT = "Garmin Explore"


@dataclass
class PositionData:
    """Represents a position report with timestamp"""
    latitude: float
    longitude: float
    altitude_m: float
    timestamp: datetime
    velocity_kmh: Optional[float] = None
    course_degrees: Optional[float] = None
    comment: Optional[str] = None

    def __str__(self):
        return f"Position({self.latitude:.6f}, {self.longitude:.6f}, alt={self.altitude_m:.1f}m, time={self.timestamp.isoformat()})"


class GarminExploreKMLParser:
    """Parser for Garmin Explore KML feeds"""

    def __init__(self, feed_url: str):
        self.feed_url = feed_url
        self.namespace = {'kml': 'http://www.opengis.net/kml/2.2'}

    def fetch_and_parse(self) -> Optional[PositionData]:
        """Fetch KML feed and parse the most recent position"""
        try:
            logger.debug(f"Fetching KML from: {self.feed_url}")
            response = requests.get(self.feed_url, timeout=30)
            response.raise_for_status()
            logger.debug(f"KML fetch successful, received {len(response.text)} bytes")
            logger.debug(f"Response status: {response.status_code}")
            return self.parse_kml(response.text)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch KML feed: {e}")
            return None

    def parse_kml(self, kml_content: str) -> Optional[PositionData]:
        """Parse KML content and extract the most recent position"""
        try:
            root = ET.fromstring(kml_content)

            # Find all Placemarks with TimeStamp (position reports, not track logs)
            placemarks = root.findall('.//kml:Placemark', self.namespace)

            latest_position = None
            latest_time = None

            for placemark in placemarks:
                # Skip track logs (LineString elements)
                if placemark.find('kml:LineString', self.namespace) is not None:
                    continue

                # Get timestamp
                timestamp_elem = placemark.find('kml:TimeStamp/kml:when', self.namespace)
                if timestamp_elem is None:
                    continue

                try:
                    timestamp = datetime.fromisoformat(timestamp_elem.text.replace('Z', '+00:00'))
                except ValueError:
                    logger.warning(f"Failed to parse timestamp: {timestamp_elem.text}")
                    continue

                # Get coordinates from Point element
                point_elem = placemark.find('kml:Point/kml:coordinates', self.namespace)
                if point_elem is None:
                    continue

                coords = point_elem.text.strip().split(',')
                if len(coords) < 2:
                    continue

                longitude = float(coords[0])
                latitude = float(coords[1])
                altitude = float(coords[2]) if len(coords) > 2 else 0.0

                # Parse extended data for velocity and course
                velocity = None
                course = None

                extended_data = placemark.find('kml:ExtendedData', self.namespace)
                if extended_data is not None:
                    for data in extended_data.findall('kml:Data', self.namespace):
                        name = data.get('name')
                        value_elem = data.find('kml:value', self.namespace)
                        if value_elem is None or not value_elem.text:
                            continue

                        if name == 'Velocity':
                            # Parse "1.0 km/h" format
                            match = re.match(r'([\d.]+)\s*km/h', value_elem.text)
                            if match:
                                velocity = float(match.group(1))
                        elif name == 'Course':
                            # Parse "22.50 ° True" format
                            match = re.match(r'([\d.]+)', value_elem.text)
                            if match:
                                course = float(match.group(1))

                # Track the most recent position
                if latest_time is None or timestamp > latest_time:
                    latest_time = timestamp
                    latest_position = PositionData(
                        latitude=latitude,
                        longitude=longitude,
                        altitude_m=altitude,
                        timestamp=timestamp,
                        velocity_kmh=velocity,
                        course_degrees=course
                    )

            if latest_position:
                logger.info(f"Parsed KML position: {latest_position}")
            else:
                logger.warning("No valid position data found in KML feed")

            return latest_position

        except ET.ParseError as e:
            logger.error(f"Failed to parse KML content: {e}")
            return None


class AprsFiClient:
    """Client for looking up the last-known position of a station via aprs.fi"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.aprs.fi/api/get"

    def get_last_position(self, callsign: str) -> Optional[PositionData]:
        """Fetch the most recent position reported for a callsign on aprs.fi.

        Returns None if nothing is found or the request fails.
        """
        params = {
            "name": callsign,
            "what": "loc",
            "apikey": self.api_key,
            "format": "json",
        }
        # aprs.fi asks that clients identify themselves with a descriptive UA.
        headers = {"User-Agent": "garmin-aprsis.py/1.0 (+https://github.com/kf6gpe)"}

        try:
            logger.debug(f"Querying aprs.fi for {callsign}...")
            response = requests.get(self.base_url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to query aprs.fi: {e}")
            return None
        except ValueError as e:
            logger.error(f"Failed to parse aprs.fi response: {e}")
            return None

        if data.get("result") != "ok":
            logger.warning(f"aprs.fi returned an error: {data.get('description', data)}")
            return None

        entries = data.get("entries") or []
        if not entries:
            logger.info(f"aprs.fi has no position on record for {callsign}")
            return None

        return self._parse_entry(entries[0])

    def _parse_entry(self, entry: Dict) -> Optional[PositionData]:
        """Convert an aprs.fi entry dict into a PositionData"""
        try:
            latitude = float(entry["lat"])
            longitude = float(entry["lng"])
            # aprs.fi timestamps are unix epoch seconds in UTC, and both of
            # these are when the packet was *heard* by the network -- not the
            # fix time carried in a timestamped (@DDHHMMz) payload. They are
            # therefore only meaningful against wall clock time, never against
            # a Garmin fix timestamp.
            epoch = int(entry.get("lasttime") or entry["time"])
            timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)

            altitude = entry.get("altitude")
            altitude_m = float(altitude) if altitude is not None else 0.0

            course = entry.get("course")
            speed = entry.get("speed")

            position = PositionData(
                latitude=latitude,
                longitude=longitude,
                altitude_m=altitude_m,
                timestamp=timestamp,
                velocity_kmh=float(speed) if speed is not None else None,
                course_degrees=float(course) if course is not None else None,
                comment=entry.get("comment"),
            )
            logger.info(f"aprs.fi last position: {position}")
            return position
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Unexpected aprs.fi entry format: {e}")
            return None


class APRSISClient:
    """APRS-IS client for sending and receiving packets"""

    def __init__(self, callsign: str, passcode: str):
        self.callsign = callsign.upper()
        self.passcode = passcode
        self.socket = None
        self.connected = False
        self._lock = threading.Lock()

    def connect(self, server_host: str = "rotate.aprs.net", server_port: int = 14580) -> bool:
        """Connect to APRS-IS server"""
        with self._lock:
            try:
                logger.debug(f"Resolving {server_host}...")
                ip_addr = socket.gethostbyname(server_host)
                logger.debug(f"Resolved to {ip_addr}")

                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(30)
                logger.debug(f"Connecting to {server_host}:{server_port}...")
                self.socket.connect((server_host, server_port))
                self.connected = True
                logger.info(f"Connected to {server_host}:{server_port}")

                # Read server banner
                try:
                    banner = self.socket.recv(1024).decode('ascii', errors='ignore')
                    logger.info(f"Server banner: {banner.strip()}")
                except socket.timeout:
                    logger.warning("No server banner received (timeout)")

                return True
            except Exception as e:
                logger.error(f"Failed to connect to {server_host}:{server_port}: {e}")
                self.connected = False
                return False

    def authenticate(self, filter_str: str = "", software: str = "GarminAPRS", version: str = "1.0") -> bool:
        """Authenticate with APRS-IS server with optional filter"""
        if not self.socket:
            logger.error("Not connected to server")
            return False

        login_string = f"user {self.callsign} pass {self.passcode} vers {software} {version}"
        if filter_str:
            login_string += f" filter {filter_str}"
        login_string += "\r\n"

        try:
            logger.debug(f"Sending login string: {login_string.strip()}")
            bytes_sent = self.socket.send(login_string.encode('ascii'))
            logger.debug(f"Sent {bytes_sent} bytes")
            logger.info(f"Authenticating as {self.callsign} with filter: {filter_str}")

            # Wait for server response
            time.sleep(1)
            response = self.socket.recv(1024).decode('ascii', errors='ignore')
            logger.info(f"Server response: {response.strip()}")

            # Check for unverified FIRST since "unverified" contains "verified"
            if "unverified" in response.lower():
                logger.warning("=" * 60)
                logger.warning("LOGIN UNVERIFIED - passcode is incorrect!")
                logger.warning("Packets will NOT be gated to the APRS-IS network!")
                logger.warning("=" * 60)
                return False  # Don't continue with bad auth
            elif "verified" in response.lower():
                logger.info("Authentication successful - VERIFIED")
                return True
            else:
                logger.warning("Authentication response unclear:")
                logger.warning(f"  Response: {response}")
                return True

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False

    def receive_line(self, timeout: float = 5.0) -> Optional[str]:
        """Receive a single line from the server"""
        if not self.socket:
            return None

        try:
            self.socket.settimeout(timeout)
            data = b""
            while True:
                chunk = self.socket.recv(1)
                if not chunk:
                    self.connected = False
                    return None
                data += chunk
                if chunk == b'\n':
                    break
            return data.decode('ascii', errors='ignore').strip()
        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"Error receiving data: {e}")
            self.connected = False
            return None

    def format_position_packet(self, callsign: str, position: PositionData,
                               symbol_table: str = "/", symbol: str = "[",
                               comment: str = "Garmin Explore") -> str:
        """Format APRS position packet

        Default symbol is /[ which is a jogger/hiker
        """

        def format_latitude(lat: float) -> str:
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - lat_deg) * 60
            lat_dir = "N" if lat >= 0 else "S"
            return f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"

        def format_longitude(lon: float) -> str:
            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - lon_deg) * 60
            lon_dir = "E" if lon >= 0 else "W"
            return f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

        lat_str = format_latitude(position.latitude)
        lon_str = format_longitude(position.longitude)

        # Build course/speed if available
        course_speed = ""
        if position.course_degrees is not None and position.velocity_kmh is not None:
            # Convert km/h to knots
            speed_knots = int(position.velocity_kmh * 0.539957)
            course_speed = f"{int(position.course_degrees):03d}/{speed_knots:03d}"

        # Build altitude if available (in feet)
        altitude_str = ""
        if position.altitude_m > 0:
            altitude_ft = int(position.altitude_m * 3.28084)
            altitude_str = f"/A={altitude_ft:06d}"

        # Format timestamp from the KML position data (already in UTC)
        # APRS timestamp format is DDHHMMz (day, hour, minute, zulu)
        ts_utc = position.timestamp.astimezone(timezone.utc)
        timestamp_str = f"{ts_utc.day:02d}{ts_utc.hour:02d}{ts_utc.minute:02d}z"

        # Build packet using @ format (position with timestamp from Garmin device)
        # Format: CALL>APRS:@DDHHMMz lat symbol_table lon symbol [/A=alt] comment
        packet = f"{callsign.upper()}>APRS:@{timestamp_str}{lat_str}{symbol_table}{lon_str}{symbol}"
        packet += altitude_str
        if comment:
            packet += f" {comment}"

        logger.debug(f"Formatted packet components:")
        logger.debug(f"  Callsign: {callsign.upper()}")
        logger.debug(f"  Timestamp: {timestamp_str} (from KML: {position.timestamp.isoformat()})")
        logger.debug(f"  Latitude: {lat_str} (from {position.latitude})")
        logger.debug(f"  Longitude: {lon_str} (from {position.longitude})")
        logger.debug(f"  Symbol: {symbol_table}{symbol}")
        logger.debug(f"  Altitude: {altitude_str if altitude_str else 'N/A'}")
        logger.debug(f"  Full packet: {packet}")

        return packet

    def send_packet(self, packet: str) -> bool:
        """Send APRS packet to server"""
        with self._lock:
            if not self.socket:
                logger.error("Not connected to server")
                return False

            try:
                packet_with_newline = packet + "\r\n"
                packet_bytes = packet_with_newline.encode('ascii')
                logger.debug(f"Sending {len(packet_bytes)} bytes: {repr(packet_bytes)}")
                bytes_sent = self.socket.send(packet_bytes)
                logger.info(f"Sent packet ({bytes_sent} bytes): {packet}")
                return True
            except Exception as e:
                logger.error(f"Failed to send packet: {e}")
                self.connected = False
                return False

    def disconnect(self):
        """Disconnect from APRS-IS server"""
        with self._lock:
            if self.socket:
                try:
                    self.socket.close()
                    logger.info("Disconnected from APRS-IS")
                except:
                    pass
                self.socket = None
                self.connected = False


class LastGatedStore:
    """Remembers the Garmin fix timestamp of the last position we gated.

    We can't recover this from aprs.fi -- it reports when a packet was heard,
    not the fix time inside it -- so a one-shot run needs to write it down.
    """

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)

    def load(self) -> Optional[datetime]:
        """Return the last gated fix time, or None if we have no record"""
        try:
            with open(self.state_path, 'r') as f:
                state = json.load(f)
            timestamp = datetime.fromisoformat(state['last_gated_fix'])
        except FileNotFoundError:
            logger.info(f"No state file at {self.state_path}, treating this as a first run")
            return None
        except (ValueError, KeyError, TypeError, OSError) as e:
            logger.warning(f"Ignoring unreadable state file {self.state_path}: {e}")
            return None

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        logger.debug(f"Last gated fix: {timestamp.isoformat()}")
        return timestamp

    def save(self, position: PositionData):
        """Record the fix we just gated"""
        state = {
            'last_gated_fix': position.timestamp.astimezone(timezone.utc).isoformat(),
            'latitude': position.latitude,
            'longitude': position.longitude,
        }
        # Write and rename, so a run killed mid-write (cron reboot, say) can't
        # leave a truncated file behind.
        tmp_path = self.state_path.with_suffix('.json.tmp')
        try:
            with open(tmp_path, 'w') as f:
                json.dump(state, f)
            os.replace(tmp_path, self.state_path)
            logger.debug(f"Recorded last gated fix in {self.state_path}")
        except OSError as e:
            # Not fatal, but it means every run re-sends the current fix, so
            # say so loudly enough to be spotted in the cron log.
            logger.error(f"Failed to write state file {self.state_path}: {e}")
            logger.error("Position de-duplication will not work until this is fixed; "
                         "the directory needs to be writable by the user running this script")
            try:
                tmp_path.unlink()
            except OSError:
                pass


class GarminAPRSBridge:
    """One-shot bridge between a Garmin Explore KML feed and APRS-IS"""

    def __init__(self, callsign: str, passcode: str, kml_feed_url: str,
                 transmit_callsign: str, aprsfi_api_key: str,
                 state_path: Path = None):
        self.callsign = callsign.upper()
        self.passcode = passcode
        self.kml_parser = GarminExploreKMLParser(kml_feed_url)
        self.transmit_callsign = transmit_callsign.upper()

        self.aprs_client = APRSISClient(callsign, passcode)
        self.aprsfi_client = AprsFiClient(aprsfi_api_key)
        if state_path is None:
            state_path = Path(__file__).parent / ".garmin-aprsis-state.json"
        self.state = LastGatedStore(state_path)

    def _connect_and_auth(self) -> bool:
        """Connect and authenticate to APRS-IS for transmitting"""
        logger.info("Connecting to APRS-IS...")
        if not self.aprs_client.connect():
            logger.error("Connection failed")
            return False

        result = self.aprs_client.authenticate()
        if result:
            logger.info("Connection and authentication complete")
        return result

    def _transmit_position(self, position: PositionData) -> bool:
        """Transmit a position to APRS-IS"""
        logger.info(f"Preparing to transmit position: {position}")
        logger.debug(f"  Source timestamp: {position.timestamp.isoformat()}")
        logger.debug(f"  Coordinates: {position.latitude}, {position.longitude}")
        logger.debug(f"  Altitude: {position.altitude_m}m")

        packet = self.aprs_client.format_position_packet(
            self.transmit_callsign,  # Transmit with the designated SSID
            position,
            comment=GATE_COMMENT
        )

        if self.aprs_client.send_packet(packet):
            logger.info(f"Successfully transmitted position for {self.transmit_callsign}")
            return True
        else:
            logger.error("Failed to transmit position - check connection")
            return False

    def run_once(self) -> bool:
        """Fetch the KML feed once, gate it to APRS-IS if newer, then return.

        Returns True if the run completed cleanly (whether or not a packet was
        sent), False on a hard error such as a failed connection.
        """
        logger.info("=" * 60)
        logger.info("Garmin APRS one-shot bridge")
        logger.info(f"  Auth callsign: {self.callsign}")
        logger.info(f"  Transmit callsign: {self.transmit_callsign}")
        logger.info("=" * 60)

        # Fetch the current position from the KML feed first; if there's nothing
        # to send there's no reason to go further.
        kml_position = self.kml_parser.fetch_and_parse()
        if kml_position is None:
            logger.warning("No KML position available, nothing to do")
            return True

        if not self._should_transmit(kml_position):
            return True

        # We have something newer to send; connect only now.
        if not self._connect_and_auth():
            logger.error("Failed to connect/authenticate to APRS-IS - aborting")
            return False

        try:
            if self._transmit_position(kml_position):
                self.state.save(kml_position)
            return True
        finally:
            self.aprs_client.disconnect()

    def _should_transmit(self, kml_position: PositionData) -> bool:
        """Decide whether this Garmin fix is newer than what's already out there.

        Two comparisons, against two clocks that can't be mixed: our own record
        of the last fix we gated (fix time vs fix time), and aprs.fi's last
        heard time for anything *else* beaconing this callsign (fix time vs
        wall clock).
        """
        # Have we already gated this exact fix? The Garmin feed repeats the
        # most recent position until the device sends a new one.
        last_gated = self.state.load()
        if last_gated is not None and kml_position.timestamp <= last_gated:
            logger.info(f"KML fix ({kml_position.timestamp.isoformat()}) is not newer than the "
                        f"last one gated ({last_gated.isoformat()}), not transmitting")
            return False

        last_aprs_position = self.aprsfi_client.get_last_position(self.transmit_callsign)

        if last_aprs_position is None:
            logger.info("No APRS position on aprs.fi, transmitting KML position")
            return True

        if last_aprs_position.comment == GATE_COMMENT:
            # This is one of our own gated packets coming back to us. Its
            # aprs.fi timestamp is when it was heard, which is always after the
            # fix time inside it, so comparing against the KML fix time would
            # wrongly reject good positions. The state file above is what
            # governs this case.
            if last_gated is None:
                # First run since the state file was introduced: don't re-gate
                # a position we can see we already sent.
                if self._same_point(kml_position, last_aprs_position):
                    logger.info("aprs.fi already shows this Garmin position, not transmitting")
                    return False
                logger.info("No local state yet and aprs.fi shows a different Garmin "
                            "position, transmitting")
            else:
                logger.info(f"KML fix ({kml_position.timestamp.isoformat()}) is newer than the "
                            f"last one gated ({last_gated.isoformat()}), transmitting")
            return True

        # Something other than this bridge is reporting for the callsign -- a
        # real radio, most likely. Its heard time is honest wall clock time, so
        # defer to it if it beat us to the punch rather than clobbering a live
        # position with a laggier Garmin fix.
        if kml_position.timestamp > last_aprs_position.timestamp:
            logger.info(f"KML position ({kml_position.timestamp.isoformat()}) is newer than the "
                        f"last APRS position ({last_aprs_position.timestamp.isoformat()}), "
                        f"transmitting")
            return True

        logger.info(f"Last APRS position ({last_aprs_position.timestamp.isoformat()}) is newer "
                    f"or equal to KML ({kml_position.timestamp.isoformat()}), not transmitting")
        return False

    @staticmethod
    def _same_point(a: PositionData, b: PositionData, tolerance_deg: float = 0.0005) -> bool:
        """Are two positions the same fix?

        The tolerance (~55 m) has to clear APRS quantization, not GPS error: an
        APRS position carries hundredths of a minute, or 0.000167 degrees, so a
        fix that survives a round trip through a packet and back out of aprs.fi
        can differ from the KML original by more than a GPS-sized epsilon.
        """
        return (abs(a.latitude - b.latitude) < tolerance_deg
                and abs(a.longitude - b.longitude) < tolerance_deg)


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
    required_fields = ['MapShareURL', 'MobileCallSign', 'Passcode', 'APRSFIApiKey']
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
    # Use base callsign (without SSID) for authentication
    mobile_callsign = config['MobileCallSign']
    base_callsign = mobile_callsign.split('-')[0] if '-' in mobile_callsign else mobile_callsign

    passcode = config['Passcode']
    kml_feed_url = config['MapShareURL']
    aprsfi_api_key = config['APRSFIApiKey']

    logger.info("Garmin Explore to APRS-IS one-shot bridge")
    logger.info(f"Transmitting as: {mobile_callsign}")
    logger.info(f"KML feed URL: {kml_feed_url}")

    bridge = GarminAPRSBridge(
        callsign=base_callsign,
        passcode=passcode,
        kml_feed_url=kml_feed_url,
        transmit_callsign=mobile_callsign,
        aprsfi_api_key=aprsfi_api_key
    )

    bridge.run_once()


if __name__ == "__main__":
    main()
