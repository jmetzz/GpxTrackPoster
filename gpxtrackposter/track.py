"""Create and maintain info about a given activity track (corresponding to one GPX file)."""
# Copyright 2016-2020 Florian Pigorsch & Contributors. All rights reserved.
#
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.

import datetime
import json
import os
import typing

import gpxpy  # type: ignore
import pint  # type: ignore
import s2sphere  # type: ignore
import polyline  # type: ignore
from stravalib.model import Activity as StravaActivity  # type: ignore

from gpxtrackposter.exceptions import TrackLoadError
from gpxtrackposter.timezone_adjuster import TimezoneAdjuster
from gpxtrackposter.units import Units


class Track:
    """Create and maintain info about a given activity track (corresponding to one GPX file).

    Attributes:
        file_names: Basename of a given file passed in load_gpx.
        polylines: Lines interpolated between each coordinate.
        start_time: Activity start time.
        end_time: Activity end time.
        length: Length of the track (2-dimensional).
        self.special: True if track is special, else False.

    Methods:
        load_gpx: Load a GPX file into the current track.
        bbox: Compute the border box of the track.
        append: Append other track to current track.
        load_cache: Load track from cached json data.
        store_cache: Cache the current track.
    """

    def __init__(self) -> None:
        self.file_names: typing.List[str] = []
        self.polylines: typing.List[typing.List[s2sphere.LatLng]] = []
        self.start_time: typing.Optional[datetime.datetime] = None
        self.end_time: typing.Optional[datetime.datetime] = None
        # Don't use Units().meter here, as this constructor is called from
        # within a thread (which would create a second unit registry!)
        self._length_meters = 0.0
        self.special = False

    def load_gpx(self, file_name: str, timezone_adjuster: typing.Optional[TimezoneAdjuster]) -> None:
        """Load the GPX file into self.

        Args:
            file_name: GPX file to be loaded .

        Raises:
            TrackLoadError: An error occurred while parsing the GPX file (empty or bad format).
            PermissionError: An error occurred while opening the GPX file.
        """
        try:
            self.file_names = [os.path.basename(file_name)]
            # Handle empty gpx files
            # (for example, treadmill runs pulled via garmin-connect-export)
            if os.path.getsize(file_name) == 0:
                raise TrackLoadError("Empty GPX file")
            with open(file_name, "r") as file:
                self._load_gpx_data(gpxpy.parse(file), timezone_adjuster)
        except TrackLoadError as e:
            raise e
        except gpxpy.gpx.GPXXMLSyntaxException as e:
            raise TrackLoadError("Failed to parse GPX.") from e
        except PermissionError as e:
            raise TrackLoadError("Cannot load GPX (bad permissions)") from e
        except Exception as e:
            raise TrackLoadError("Something went wrong when loading GPX.") from e

    def load_strava(self, activate: StravaActivity) -> None:
        # use strava as file name
        self.file_names = [str(activate.id)]
        self.start_time = activate.start_date_local
        self.end_time = activate.start_date_local + activate.elapsed_time
        self._length_meters = float(activate.distance)
        summary_polyline = activate.map.summary_polyline
        polyline_data = polyline.decode(summary_polyline) if summary_polyline else []
        self.polylines = [[s2sphere.LatLng.from_degrees(p[0], p[1]) for p in polyline_data]]

    @property
    def length_meters(self) -> float:
        return self._length_meters

    @length_meters.setter
    def length_meters(self, value: float) -> None:
        self._length_meters = value

    def length(self) -> pint.quantity.Quantity:
        return self._length_meters * Units().meter

    def bbox(self) -> s2sphere.LatLngRect:
        """Compute the smallest rectangle that contains the entire track (border box)."""
        bbox = s2sphere.LatLngRect()
        for line in self.polylines:
            for latlng in line:
                bbox = bbox.union(s2sphere.LatLngRect.from_point(latlng.normalized()))
        return bbox

    def _load_gpx_data(self, gpx: gpxpy.gpx.GPX, timezone_adjuster: typing.Optional[TimezoneAdjuster]) -> None:
        self.start_time, self.end_time = gpx.get_time_bounds()
        if self.start_time is None:
            raise TrackLoadError("Track has no start time.")
        if self.end_time is None:
            raise TrackLoadError("Track has no end time.")
        if timezone_adjuster:
            lat, _, lng, _ = list(gpx.get_bounds())
            latlng = s2sphere.LatLng.from_degrees(lat, lng)
            self.start_time = timezone_adjuster.adjust(self.start_time, latlng)
            self.end_time = timezone_adjuster.adjust(self.end_time, latlng)
        self._length_meters = gpx.length_2d()
        if self._length_meters <= 0:
            raise TrackLoadError("Track is empty.")
        gpx.simplify()
        for t in gpx.tracks:
            for s in t.segments:
                line = [s2sphere.LatLng.from_degrees(p.latitude, p.longitude) for p in s.points]
                self.polylines.append(line)

    def append(self, other: "Track") -> None:
        """Append other track to self."""
        self.end_time = other.end_time
        self.polylines.extend(other.polylines)
        self._length_meters += other.length_meters
        self.file_names.extend(other.file_names)
        self.special = self.special or other.special

    def load_cache(self, cache_file_name: str) -> None:
        """Load the track from a previously cached track

        Args:
            cache_file_name: Filename of the cached track to be loaded.

        Raises:
            TrackLoadError: An error occurred while loading the track data from the cache file.
        """
        try:
            with open(cache_file_name) as data_file:
                data = json.load(data_file)
                self.start_time = datetime.datetime.strptime(data["start"], "%Y-%m-%d %H:%M:%S")
                self.end_time = datetime.datetime.strptime(data["end"], "%Y-%m-%d %H:%M:%S")
                self._length_meters = float(data["length"])
                self.polylines = []
                for data_line in data["segments"]:
                    self.polylines.append(
                        [s2sphere.LatLng.from_degrees(float(d["lat"]), float(d["lng"])) for d in data_line]
                    )
        except Exception as e:
            raise TrackLoadError("Failed to load track data from cache.") from e

    def store_cache(self, cache_file_name: str) -> None:
        """Cache the current track"""
        dir_name = os.path.dirname(cache_file_name)
        if not os.path.isdir(dir_name):
            os.makedirs(dir_name)
        with open(cache_file_name, "w") as json_file:
            lines_data = []
            for line in self.polylines:
                lines_data.append([{"lat": latlng.lat().degrees, "lng": latlng.lng().degrees} for latlng in line])
            assert self.start_time is not None
            assert self.end_time is not None
            json.dump(
                {
                    "start": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end": self.end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "length": self._length_meters,
                    "segments": lines_data,
                },
                json_file,
            )
