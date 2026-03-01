# python-aprs-microservices

A set of simple microservices for APRS integration to other
Web-based services. These scripts interface with Web services
and proxy data to the APRS-IS stream for Automated Packet
Reporting System. 

To use these, you'll need an Amateur Radio license and a APRS
passcode to access the APRIS-IS network of servers.

You'll need python3 installed to run these scripts, and
they've only been tested under Linux. (They're known to work
on a Google Cloud VM, which is where I host them for
kf6gpe.org).

## The Garmin Map Explore Service Bridge.

`garmin-apris-bridge.py`

This script runs a microservice that will query a Garmin Map
Explore service periodically for a location packet to proxy
to the APRS-IS stream.

Garmin Map Explore is the Web service endpoint for Garmin's
inReach family of satellite messaging products. This script
has been tested with a Garmin Montana 700.

The microservice polls the Map Explore service every five
minutes for the most recent position reported by the inReach
communicator, and transmits a position beacon via APRS-IS
if the position reported by the Map Explore service is more
recent than the last reported APRS packet.

This script should be run at boot, using something like
this in your crontab:

```
@reboot (cd /home/kf6gpe ; /home/kf6gpe/garmin-aprsis-bridge.py &)
```

## Tempest Weather Station Service Utility

Tempest provides a back-end Web API for their series of
home and professional weather stations. This utility
script accesses the Web API to fetch the weather data
for a specific weather station, connects to the APRS-IS
network, and beacons the weather station's data and
position.

It runs one-shot; to run it as a service you'll need
to establish a crontab entry like this:

```
*/30 * * * *  /home/kf6gpe/python-tempestwx-aprsis.py
```

This will run it every ten minutes. You should use a sensible interval for
this so as not to flood the APRS-IS service with position and weather
data.

## Configuration

To configure the scripts, edit `config.yaml` with the following
information:

- `MapShareURL`. The Map Share URL provided by [Garmin Explore](https://explore.garmin.com/Social)
- `TempestStationID`. Your Tempest Station ID.
- `TempestAPIKey`. Your Tempest API key, which you can get from the
[Tempest Developer Web site](https://explore.garmin.com/Social)
- `WXCallSign`. The callsign you'd like to use for your weather station.
- `MobileCallSign`. The callsign you'd like to use for the Map Explore position
reporting.
- `Passcode`. Your APRS passcode.
