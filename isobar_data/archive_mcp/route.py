"""Constant-distance sampling of the shortest great-circle route legs."""
from __future__ import annotations

import bisect
import math

import numpy as np

from .archive import coordinate

EARTH_RADIUS_KM = 6371.0088


def route_points(points, samples: int) -> list[tuple[float, float, float]]:
    if type(samples) is not int or not 2 <= samples <= 2000:
        raise ValueError('samples must be an integer from 2 through 2000')
    if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 1000:
        raise ValueError('points must contain 2 through 1000 [lat,lon] pairs')
    coords = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('each point must be [lat,lon]')
        coords.append(coordinate(*point))
    vectors = []
    for lat, lon in coords:
        phi, lam = math.radians(lat), math.radians(lon)
        vectors.append(np.array([math.cos(phi)*math.cos(lam), math.cos(phi)*math.sin(lam), math.sin(phi)]))
    lengths = [0.0]
    angles = []
    for a,b in zip(vectors, vectors[1:]):
        angle = math.atan2(float(np.linalg.norm(np.cross(a,b))), float(np.dot(a,b)))
        if math.pi-angle < 1e-8:
            raise ValueError('antipodal route points need an intermediate waypoint')
        angles.append(angle)
        lengths.append(lengths[-1]+angle)
    result = []
    for i in range(samples):
        distance = lengths[-1]*i/(samples-1)
        leg = min(bisect.bisect_right(lengths, distance)-1,len(angles)-1)
        angle = angles[leg]
        fraction = 0 if angle < 1e-12 else (distance-lengths[leg])/angle
        a,b = vectors[leg:leg+2]
        v = a if angle < 1e-12 else (math.sin((1-fraction)*angle)*a+math.sin(fraction*angle)*b)/math.sin(angle)
        lat = math.degrees(math.atan2(float(v[2]), math.hypot(float(v[0]),float(v[1]))))
        lon = math.degrees(math.atan2(float(v[1]),float(v[0])))
        result.append((distance*EARTH_RADIUS_KM,lat,lon))
    # Preserve the submitted endpoints exactly, including the +/-180 convention.
    result[0] = (0.0,*coords[0])
    result[-1] = (lengths[-1]*EARTH_RADIUS_KM,*coords[-1])
    return result
