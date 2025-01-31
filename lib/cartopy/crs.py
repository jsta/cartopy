# Copyright Cartopy Contributors
#
# This file is part of Cartopy and is released under the LGPL license.
# See COPYING and COPYING.LESSER in the root of the repository for full
# licensing details.

"""
The crs module defines Coordinate Reference Systems and the transformations
between them.

"""

from abc import ABCMeta, abstractproperty
import io
import math
import warnings

import numpy as np
import shapely.geometry as sgeom
from shapely.prepared import prep

from cartopy._crs import (CRS, Geodetic, Globe, PROJ4_VERSION,
                          WGS84_SEMIMAJOR_AXIS, WGS84_SEMIMINOR_AXIS)
from cartopy._crs import Geocentric  # noqa: F401 (flake8 = unused import)
import cartopy.trace


__document_these__ = ['CRS', 'Geocentric', 'Geodetic', 'Globe']


class RotatedGeodetic(CRS):
    """
    Define a rotated latitude/longitude coordinate system with spherical
    topology and geographical distance.

    Coordinates are measured in degrees.

    The class uses proj to perform an ob_tran operation, using the
    pole_longitude to set a lon_0 then performing two rotations based on
    pole_latitude and central_rotated_longitude.
    This is equivalent to setting the new pole to a location defined by
    the pole_latitude and pole_longitude values in the GeogCRS defined by
    globe, then rotating this new CRS about it's pole using the
    central_rotated_longitude value.

    """
    def __init__(self, pole_longitude, pole_latitude,
                 central_rotated_longitude=0.0, globe=None):
        """
        Parameters
        ----------
        pole_longitude
            Pole longitude position, in unrotated degrees.
        pole_latitude
            Pole latitude position, in unrotated degrees.
        central_rotated_longitude: optional
            Longitude rotation about the new pole, in degrees.  Defaults to 0.
        globe: optional
            A :class:`cartopy.crs.Globe`.  Defaults to a "WGS84" datum.

        """
        proj4_params = [('proj', 'ob_tran'), ('o_proj', 'latlon'),
                        ('o_lon_p', central_rotated_longitude),
                        ('o_lat_p', pole_latitude),
                        ('lon_0', 180 + pole_longitude),
                        ('to_meter', math.radians(1))]
        globe = globe or Globe(datum='WGS84')
        super().__init__(proj4_params, globe=globe)


class Projection(CRS, metaclass=ABCMeta):
    """
    Define a projected coordinate system with flat topology and Euclidean
    distance.

    """

    _method_map = {
        'Point': '_project_point',
        'LineString': '_project_line_string',
        'LinearRing': '_project_linear_ring',
        'Polygon': '_project_polygon',
        'MultiPoint': '_project_multipoint',
        'MultiLineString': '_project_multiline',
        'MultiPolygon': '_project_multipolygon',
    }

    @abstractproperty
    def boundary(self):
        pass

    @abstractproperty
    def x_limits(self):
        pass

    @abstractproperty
    def y_limits(self):
        pass

    @property
    def threshold(self):
        return getattr(self, '_threshold', 0.5)

    @threshold.setter
    def threshold(self, t):
        self._threshold = t

    @property
    def cw_boundary(self):
        try:
            boundary = self._cw_boundary
        except AttributeError:
            boundary = sgeom.LinearRing(self.boundary)
            self._cw_boundary = boundary
        return boundary

    @property
    def ccw_boundary(self):
        try:
            boundary = self._ccw_boundary
        except AttributeError:
            boundary = sgeom.LinearRing(self.boundary.coords[::-1])
            self._ccw_boundary = boundary
        return boundary

    @property
    def domain(self):
        try:
            domain = self._domain
        except AttributeError:
            domain = self._domain = sgeom.Polygon(self.boundary)
        return domain

    def _determine_longitude_bounds(self, central_longitude):
        # In new proj, using exact limits will wrap-around, so subtract a
        # small epsilon:
        epsilon = 1e-10
        minlon = -180 + central_longitude
        maxlon = 180 + central_longitude
        if central_longitude > 0:
            maxlon -= epsilon
        elif central_longitude < 0:
            minlon += epsilon
        return minlon, maxlon

    def _repr_html_(self):
        from html import escape
        try:
            # As matplotlib is not a core cartopy dependency, don't error
            # if it's not available.
            import matplotlib.pyplot as plt
        except ImportError:
            # We can't return an SVG of the CRS, so let Jupyter fall back to
            # a default repr by returning None.
            return None

        # Produce a visual repr of the Projection instance.
        fig, ax = plt.subplots(figsize=(5, 3),
                               subplot_kw={'projection': self})
        ax.set_global()
        ax.coastlines('auto')
        ax.gridlines()
        buf = io.StringIO()
        fig.savefig(buf, format='svg', bbox_inches='tight')
        plt.close(fig)
        # "Rewind" the buffer to the start and return it as an svg string.
        buf.seek(0)
        svg = buf.read()
        return '{}<pre>{}</pre>'.format(svg, escape(repr(self)))

    def _as_mpl_axes(self):
        import cartopy.mpl.geoaxes as geoaxes
        return geoaxes.GeoAxes, {'map_projection': self}

    def project_geometry(self, geometry, src_crs=None):
        """
        Project the given geometry into this projection.

        Parameters
        ----------
        geometry
            The geometry to (re-)project.
        src_crs: optional
            The source CRS.  Defaults to None.

            If src_crs is None, the source CRS is assumed to be a geodetic
            version of the target CRS.

        Returns
        -------
        geometry
            The projected result (a shapely geometry).

        """
        if src_crs is None:
            src_crs = self.as_geodetic()
        elif not isinstance(src_crs, CRS):
            raise TypeError('Source CRS must be an instance of CRS'
                            ' or one of its subclasses, or None.')
        geom_type = geometry.geom_type
        method_name = self._method_map.get(geom_type)
        if not method_name:
            raise ValueError('Unsupported geometry '
                             'type {!r}'.format(geom_type))
        return getattr(self, method_name)(geometry, src_crs)

    def _project_point(self, point, src_crs):
        return sgeom.Point(*self.transform_point(point.x, point.y, src_crs))

    def _project_line_string(self, geometry, src_crs):
        return cartopy.trace.project_linear(geometry, src_crs, self)

    def _project_linear_ring(self, linear_ring, src_crs):
        """
        Project the given LinearRing from the src_crs into this CRS and
        returns a list of LinearRings and a single MultiLineString.

        """
        debug = False
        # 1) Resolve the initial lines into projected segments
        # 1abc
        # def23ghi
        # jkl41
        multi_line_string = cartopy.trace.project_linear(linear_ring,
                                                         src_crs, self)

        # Threshold for whether a point is close enough to be the same
        # point as another.
        threshold = max(np.abs(self.x_limits + self.y_limits)) * 1e-5

        # 2) Simplify the segments where appropriate.
        if len(multi_line_string) > 1:
            # Stitch together segments which are close to continuous.
            # This is important when:
            # 1) The first source point projects into the map and the
            # ring has been cut by the boundary.
            # Continuing the example from above this gives:
            #   def23ghi
            #   jkl41abc
            # 2) The cut ends of segments are too close to reliably
            # place into an order along the boundary.

            line_strings = list(multi_line_string)
            any_modified = False
            i = 0
            if debug:
                first_coord = np.array([ls.coords[0] for ls in line_strings])
                last_coord = np.array([ls.coords[-1] for ls in line_strings])
                print('Distance matrix:')
                np.set_printoptions(precision=2)
                x = first_coord[:, np.newaxis, :]
                y = last_coord[np.newaxis, :, :]
                print(np.abs(x - y).max(axis=-1))

            while i < len(line_strings):
                modified = False
                j = 0
                while j < len(line_strings):
                    if i != j and np.allclose(line_strings[i].coords[0],
                                              line_strings[j].coords[-1],
                                              atol=threshold):
                        if debug:
                            print('Joining together {} and {}.'.format(i, j))
                        last_coords = list(line_strings[j].coords)
                        first_coords = list(line_strings[i].coords)[1:]
                        combo = sgeom.LineString(last_coords + first_coords)
                        if j < i:
                            i, j = j, i
                        del line_strings[j], line_strings[i]
                        line_strings.append(combo)
                        modified = True
                        any_modified = True
                        break
                    else:
                        j += 1
                if not modified:
                    i += 1
            if any_modified:
                multi_line_string = sgeom.MultiLineString(line_strings)

        # 3) Check for rings that have been created by the projection stage.
        rings = []
        line_strings = []
        for line in multi_line_string:
            if len(line.coords) > 3 and np.allclose(line.coords[0],
                                                    line.coords[-1],
                                                    atol=threshold):
                result_geometry = sgeom.LinearRing(line.coords[:-1])
                rings.append(result_geometry)
            else:
                line_strings.append(line)
        # If we found any rings, then we should re-create the multi-line str.
        if rings:
            multi_line_string = sgeom.MultiLineString(line_strings)

        return rings, multi_line_string

    def _project_multipoint(self, geometry, src_crs):
        geoms = []
        for geom in geometry.geoms:
            geoms.append(self._project_point(geom, src_crs))
        if geoms:
            return sgeom.MultiPoint(geoms)
        else:
            return sgeom.MultiPoint()

    def _project_multiline(self, geometry, src_crs):
        geoms = []
        for geom in geometry.geoms:
            r = self._project_line_string(geom, src_crs)
            if r:
                geoms.extend(r.geoms)
        if geoms:
            return sgeom.MultiLineString(geoms)
        else:
            return []

    def _project_multipolygon(self, geometry, src_crs):
        geoms = []
        for geom in geometry.geoms:
            r = self._project_polygon(geom, src_crs)
            if r:
                geoms.extend(r.geoms)
        if geoms:
            result = sgeom.MultiPolygon(geoms)
        else:
            result = sgeom.MultiPolygon()
        return result

    def _project_polygon(self, polygon, src_crs):
        """
        Return the projected polygon(s) derived from the given polygon.

        """
        # Determine orientation of polygon.
        # TODO: Consider checking the internal rings have the opposite
        # orientation to the external rings?
        if src_crs.is_geodetic():
            is_ccw = True
        else:
            is_ccw = polygon.exterior.is_ccw
        # Project the polygon exterior/interior rings.
        # Each source ring will result in either a ring, or one or more
        # lines.
        rings = []
        multi_lines = []
        for src_ring in [polygon.exterior] + list(polygon.interiors):
            p_rings, p_mline = self._project_linear_ring(src_ring, src_crs)
            if p_rings:
                rings.extend(p_rings)
            if len(p_mline) > 0:
                multi_lines.append(p_mline)

        # Convert any lines to rings by attaching them to the boundary.
        if multi_lines:
            rings.extend(self._attach_lines_to_boundary(multi_lines, is_ccw))

        # Resolve all the inside vs. outside rings, and convert to the
        # final MultiPolygon.
        return self._rings_to_multi_polygon(rings, is_ccw)

    def _attach_lines_to_boundary(self, multi_line_strings, is_ccw):
        """
        Return a list of LinearRings by attaching the ends of the given lines
        to the boundary, paying attention to the traversal directions of the
        lines and boundary.

        """
        debug = False
        debug_plot_edges = False

        # Accumulate all the boundary and segment end points, along with
        # their distance along the boundary.
        edge_things = []

        # Get the boundary as a LineString of the correct orientation
        # so we can compute distances along it.
        if is_ccw:
            boundary = self.ccw_boundary
        else:
            boundary = self.cw_boundary

        def boundary_distance(xy):
            return boundary.project(sgeom.Point(*xy))

        # Squash all the LineStrings into a single list.
        line_strings = []
        for multi_line_string in multi_line_strings:
            line_strings.extend(multi_line_string)

        # Record the positions of all the segment ends
        for i, line_string in enumerate(line_strings):
            first_dist = boundary_distance(line_string.coords[0])
            thing = _BoundaryPoint(first_dist, False,
                                   (i, 'first', line_string.coords[0]))
            edge_things.append(thing)
            last_dist = boundary_distance(line_string.coords[-1])
            thing = _BoundaryPoint(last_dist, False,
                                   (i, 'last', line_string.coords[-1]))
            edge_things.append(thing)

        # Record the positions of all the boundary vertices
        for xy in boundary.coords[:-1]:
            point = sgeom.Point(*xy)
            dist = boundary.project(point)
            thing = _BoundaryPoint(dist, True, point)
            edge_things.append(thing)

        if debug_plot_edges:
            import matplotlib.pyplot as plt
            current_fig = plt.gcf()
            fig = plt.figure()
            # Reset the current figure so we don't upset anything.
            plt.figure(current_fig.number)
            ax = fig.add_subplot(1, 1, 1)

        # Order everything as if walking around the boundary.
        # NB. We make line end-points take precedence over boundary points
        # to ensure that end-points are still found and followed when they
        # coincide.
        edge_things.sort(key=lambda thing: (thing.distance, thing.kind))
        remaining_ls = dict(enumerate(line_strings))

        prev_thing = None
        for edge_thing in edge_things[:]:
            if (prev_thing is not None and
                    not edge_thing.kind and
                    not prev_thing.kind and
                    edge_thing.data[0] == prev_thing.data[0]):
                j = edge_thing.data[0]
                # Insert a edge boundary point in between this geometry.
                mid_dist = (edge_thing.distance + prev_thing.distance) * 0.5
                mid_point = boundary.interpolate(mid_dist)
                new_thing = _BoundaryPoint(mid_dist, True, mid_point)
                if debug:
                    print('Artificially insert boundary: {}'.format(new_thing))
                ind = edge_things.index(edge_thing)
                edge_things.insert(ind, new_thing)
                prev_thing = None
            else:
                prev_thing = edge_thing

        if debug:
            print()
            print('Edge things')
            for thing in edge_things:
                print('   ', thing)
        if debug_plot_edges:
            for thing in edge_things:
                if isinstance(thing.data, sgeom.Point):
                    ax.plot(*thing.data.xy, marker='o')
                else:
                    ax.plot(*thing.data[2], marker='o')
                    ls = line_strings[thing.data[0]]
                    coords = np.array(ls.coords)
                    ax.plot(coords[:, 0], coords[:, 1])
                    ax.text(coords[0, 0], coords[0, 1], thing.data[0])
                    ax.text(coords[-1, 0], coords[-1, 1],
                            '{}.'.format(thing.data[0]))

        def filter_last(t):
            return t.kind or t.data[1] == 'first'

        edge_things = list(filter(filter_last, edge_things))

        processed_ls = []
        while remaining_ls:
            # Rename line_string to current_ls
            i, current_ls = remaining_ls.popitem()

            if debug:
                import sys
                sys.stdout.write('+')
                sys.stdout.flush()
                print()
                print('Processing: {}, {}'.format(i, current_ls))

            added_linestring = set()
            while True:
                # Find out how far around this linestring's last
                # point is on the boundary. We will use this to find
                # the next point on the boundary.
                d_last = boundary_distance(current_ls.coords[-1])
                if debug:
                    print('   d_last: {!r}'.format(d_last))
                next_thing = _find_first_ge(edge_things, d_last)
                # Remove this boundary point from the edge.
                edge_things.remove(next_thing)
                if debug:
                    print('   next_thing:', next_thing)
                if next_thing.kind:
                    # We've just got a boundary point, add it, and keep going.
                    if debug:
                        print('   adding boundary point')
                    boundary_point = next_thing.data
                    combined_coords = (list(current_ls.coords) +
                                       [(boundary_point.x, boundary_point.y)])
                    current_ls = sgeom.LineString(combined_coords)

                elif next_thing.data[0] == i:
                    # We've gone all the way around and are now back at the
                    # first boundary thing.
                    if debug:
                        print('   close loop')
                    processed_ls.append(current_ls)
                    if debug_plot_edges:
                        coords = np.array(current_ls.coords)
                        ax.plot(coords[:, 0], coords[:, 1], color='black',
                                linestyle='--')
                    break
                else:
                    if debug:
                        print('   adding line')
                    j = next_thing.data[0]
                    line_to_append = line_strings[j]
                    if j in remaining_ls:
                        remaining_ls.pop(j)
                    coords_to_append = list(line_to_append.coords)

                    # Build up the linestring.
                    current_ls = sgeom.LineString(list(current_ls.coords) +
                                                  coords_to_append)

                    # Catch getting stuck in an infinite loop by checking that
                    # linestring only added once.
                    if j not in added_linestring:
                        added_linestring.add(j)
                    else:
                        if debug_plot_edges:
                            plt.show()
                        raise RuntimeError('Unidentified problem with '
                                           'geometry, linestring being '
                                           're-added. Please raise an issue.')

        # filter out any non-valid linear rings
        def makes_valid_ring(line_string):
            if len(line_string.coords) == 3:
                # When sgeom.LinearRing is passed a LineString of length 3,
                # if the first and last coordinate are equal, a LinearRing
                # with 3 coordinates will be created. This object will cause
                # a segfault when evaluated.
                coords = list(line_string.coords)
                return coords[0] != coords[-1] and line_string.is_valid
            else:
                return len(line_string.coords) > 3 and line_string.is_valid

        linear_rings = [
            sgeom.LinearRing(line_string)
            for line_string in processed_ls
            if makes_valid_ring(line_string)]

        if debug:
            print('   DONE')

        return linear_rings

    def _rings_to_multi_polygon(self, rings, is_ccw):
        exterior_rings = []
        interior_rings = []
        for ring in rings:
            if ring.is_ccw != is_ccw:
                interior_rings.append(ring)
            else:
                exterior_rings.append(ring)

        polygon_bits = []

        # Turn all the exterior rings into polygon definitions,
        # "slurping up" any interior rings they contain.
        for exterior_ring in exterior_rings:
            polygon = sgeom.Polygon(exterior_ring)
            prep_polygon = prep(polygon)
            holes = []
            for interior_ring in interior_rings[:]:
                if prep_polygon.contains(interior_ring):
                    holes.append(interior_ring)
                    interior_rings.remove(interior_ring)
                elif polygon.crosses(interior_ring):
                    # Likely that we have an invalid geometry such as
                    # that from #509 or #537.
                    holes.append(interior_ring)
                    interior_rings.remove(interior_ring)
            polygon_bits.append((exterior_ring.coords,
                                 [ring.coords for ring in holes]))

        # Any left over "interior" rings need "inverting" with respect
        # to the boundary.
        if interior_rings:
            boundary_poly = self.domain
            x3, y3, x4, y4 = boundary_poly.bounds
            bx = (x4 - x3) * 0.1
            by = (y4 - y3) * 0.1
            x3 -= bx
            y3 -= by
            x4 += bx
            y4 += by
            for ring in interior_rings:
                # Use shapely buffer in an attempt to fix invalid geometries
                polygon = sgeom.Polygon(ring).buffer(0)
                if not polygon.is_empty and polygon.is_valid:
                    x1, y1, x2, y2 = polygon.bounds
                    bx = (x2 - x1) * 0.1
                    by = (y2 - y1) * 0.1
                    x1 -= bx
                    y1 -= by
                    x2 += bx
                    y2 += by
                    box = sgeom.box(min(x1, x3), min(y1, y3),
                                    max(x2, x4), max(y2, y4))

                    # Invert the polygon
                    polygon = box.difference(polygon)

                    # Intersect the inverted polygon with the boundary
                    polygon = boundary_poly.intersection(polygon)

                    if not polygon.is_empty:
                        polygon_bits.append(polygon)

        if polygon_bits:
            multi_poly = sgeom.MultiPolygon(polygon_bits)
        else:
            multi_poly = sgeom.MultiPolygon()
        return multi_poly

    def quick_vertices_transform(self, vertices, src_crs):
        """
        Where possible, return a vertices array transformed to this CRS from
        the given vertices array of shape ``(n, 2)`` and the source CRS.

        Note
        ----
            This method may return None to indicate that the vertices cannot
            be transformed quickly, and a more complex geometry transformation
            is required (see :meth:`cartopy.crs.Projection.project_geometry`).

        """
        return_value = None

        if self == src_crs:
            x = vertices[:, 0]
            y = vertices[:, 1]
            # Extend the limits a tiny amount to allow for precision mistakes
            epsilon = 1.e-10
            x_limits = (self.x_limits[0] - epsilon, self.x_limits[1] + epsilon)
            y_limits = (self.y_limits[0] - epsilon, self.y_limits[1] + epsilon)
            if (x.min() >= x_limits[0] and x.max() <= x_limits[1] and
                    y.min() >= y_limits[0] and y.max() <= y_limits[1]):
                return_value = vertices

        return return_value


class _RectangularProjection(Projection, metaclass=ABCMeta):
    """
    The abstract superclass of projections with a rectangular domain which
    is symmetric about the origin.

    """
    def __init__(self, proj4_params, half_width, half_height, globe=None):
        self._half_width = half_width
        self._half_height = half_height
        super().__init__(proj4_params, globe=globe)

    @property
    def boundary(self):
        w, h = self._half_width, self._half_height
        return sgeom.LinearRing([(-w, -h), (-w, h), (w, h), (w, -h), (-w, -h)])

    @property
    def x_limits(self):
        return (-self._half_width, self._half_width)

    @property
    def y_limits(self):
        return (-self._half_height, self._half_height)


class _CylindricalProjection(_RectangularProjection, metaclass=ABCMeta):
    """
    The abstract class which denotes cylindrical projections where we
    want to allow x values to wrap around.

    """


def _ellipse_boundary(semimajor=2, semiminor=1, easting=0, northing=0, n=201):
    """
    Define a projection boundary using an ellipse.

    This type of boundary is used by several projections.

    """

    t = np.linspace(0, -2 * np.pi, n)  # Clockwise boundary.
    coords = np.vstack([semimajor * np.cos(t), semiminor * np.sin(t)])
    coords += ([easting], [northing])
    return coords


class PlateCarree(_CylindricalProjection):
    def __init__(self, central_longitude=0.0, globe=None):
        proj4_params = [('proj', 'eqc'), ('lon_0', central_longitude)]
        if globe is None:
            globe = Globe(semimajor_axis=math.degrees(1))
        a_rad = math.radians(globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)
        x_max = a_rad * 180
        y_max = a_rad * 90
        # Set the threshold around 0.5 if the x max is 180.
        self.threshold = x_max / 360
        super().__init__(proj4_params, x_max, y_max, globe=globe)

    def _bbox_and_offset(self, other_plate_carree):
        """
        Return a pair of (xmin, xmax) pairs and an offset which can be used
        for identification of whether data in ``other_plate_carree`` needs
        to be transformed to wrap appropriately.

        >>> import cartopy.crs as ccrs
        >>> src = ccrs.PlateCarree(central_longitude=10)
        >>> bboxes, offset = ccrs.PlateCarree()._bbox_and_offset(src)
        >>> print(bboxes)
        [[-180.0, -170.0], [-170.0, 180.0]]
        >>> print(offset)
        10.0

        The returned values are longitudes in ``other_plate_carree``'s
        coordinate system.

        Warning
        -------
            The two CRSs must be identical in every way, other than their
            central longitudes. No checking of this is done.

        """
        self_lon_0 = self.proj4_params['lon_0']
        other_lon_0 = other_plate_carree.proj4_params['lon_0']

        lon_0_offset = other_lon_0 - self_lon_0

        lon_lower_bound_0 = self.x_limits[0]
        lon_lower_bound_1 = (other_plate_carree.x_limits[0] + lon_0_offset)

        if lon_lower_bound_1 < self.x_limits[0]:
            lon_lower_bound_1 += np.diff(self.x_limits)[0]

        lon_lower_bound_0, lon_lower_bound_1 = sorted(
            [lon_lower_bound_0, lon_lower_bound_1])

        bbox = [[lon_lower_bound_0, lon_lower_bound_1],
                [lon_lower_bound_1, lon_lower_bound_0]]

        bbox[1][1] += np.diff(self.x_limits)[0]

        return bbox, lon_0_offset

    def quick_vertices_transform(self, vertices, src_crs):
        return_value = super().quick_vertices_transform(vertices, src_crs)

        # Optimise the PlateCarree -> PlateCarree case where no
        # wrapping or interpolation needs to take place.
        if return_value is None and isinstance(src_crs, PlateCarree):
            self_params = self.proj4_params.copy()
            src_params = src_crs.proj4_params.copy()
            self_params.pop('lon_0'), src_params.pop('lon_0')

            xs, ys = vertices[:, 0], vertices[:, 1]

            potential = (self_params == src_params and
                         self.y_limits[0] <= ys.min() and
                         self.y_limits[1] >= ys.max())

            if potential:
                mod = np.diff(src_crs.x_limits)[0]
                bboxes, proj_offset = self._bbox_and_offset(src_crs)
                x_lim = xs.min(), xs.max()
                for poly in bboxes:
                    # Arbitrarily choose the number of moduli to look
                    # above and below the -180->180 range. If data is beyond
                    # this range, we're not going to transform it quickly.
                    for i in [-1, 0, 1, 2]:
                        offset = mod * i - proj_offset
                        if ((poly[0] + offset) <= x_lim[0] and
                                (poly[1] + offset) >= x_lim[1]):
                            return_value = vertices + [[-offset, 0]]
                            break
                    if return_value is not None:
                        break

        return return_value


class TransverseMercator(Projection):
    """
    A Transverse Mercator projection.

    """
    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 scale_factor=1.0, globe=None, approx=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The true longitude of the central meridian in degrees.
            Defaults to 0.
        central_latitude: optional
            The true latitude of the planar origin in degrees. Defaults to 0.
        false_easting: optional
            X offset from the planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from the planar origin in metres. Defaults to 0.
        scale_factor: optional
            Scale factor at the central meridian. Defaults to 1.

        globe: optional
            An instance of :class:`cartopy.crs.Globe`. If omitted, a default
            globe is created.

        approx: optional
            Whether to use Proj's approximate projection (True), or the new
            Extended Transverse Mercator code (False). Defaults to True, but
            will change to False in the next release.

        """
        if approx is None:
            warnings.warn('The default value for the *approx* keyword '
                          'argument to TransverseMercator will change '
                          'from True to False after 0.18.',
                          stacklevel=2)
            approx = True
        proj4_params = [('proj', 'tmerc'), ('lon_0', central_longitude),
                        ('lat_0', central_latitude), ('k', scale_factor),
                        ('x_0', false_easting), ('y_0', false_northing),
                        ('units', 'm')]
        if PROJ4_VERSION < (6, 0, 0):
            if not approx:
                proj4_params[0] = ('proj', 'etmerc')
        else:
            if approx:
                proj4_params += [('approx', None)]
        super().__init__(proj4_params, globe=globe)

        self.threshold = 1e4

    @property
    def boundary(self):
        x0, x1 = self.x_limits
        y0, y1 = self.y_limits
        return sgeom.LinearRing([(x0, y0), (x0, y1),
                                 (x1, y1), (x1, y0),
                                 (x0, y0)])

    @property
    def x_limits(self):
        return (-2e7, 2e7)

    @property
    def y_limits(self):
        return (-1e7, 1e7)


class OSGB(TransverseMercator):
    def __init__(self, approx=None):
        if approx is None:
            warnings.warn('The default value for the *approx* keyword '
                          'argument to OSGB will change from True to '
                          'False after 0.18.',
                          stacklevel=2)
            approx = True
        super().__init__(central_longitude=-2, central_latitude=49,
                         scale_factor=0.9996012717,
                         false_easting=400000, false_northing=-100000,
                         globe=Globe(datum='OSGB36', ellipse='airy'),
                         approx=approx)

    @property
    def boundary(self):
        w = self.x_limits[1] - self.x_limits[0]
        h = self.y_limits[1] - self.y_limits[0]
        return sgeom.LinearRing([(0, 0), (0, h), (w, h), (w, 0), (0, 0)])

    @property
    def x_limits(self):
        return (0, 7e5)

    @property
    def y_limits(self):
        return (0, 13e5)


class OSNI(TransverseMercator):
    def __init__(self, approx=None):
        if approx is None:
            warnings.warn('The default value for the *approx* keyword '
                          'argument to OSNI will change from True to '
                          'False after 0.18.',
                          stacklevel=2)
            approx = True
        globe = Globe(semimajor_axis=6377340.189,
                      semiminor_axis=6356034.447938534)
        super().__init__(central_longitude=-8, central_latitude=53.5,
                         scale_factor=1.000035,
                         false_easting=200000, false_northing=250000,
                         globe=globe, approx=approx)

    @property
    def boundary(self):
        w = self.x_limits[1] - self.x_limits[0]
        h = self.y_limits[1] - self.y_limits[0]
        return sgeom.LinearRing([(0, 0), (0, h), (w, h), (w, 0), (0, 0)])

    @property
    def x_limits(self):
        return (18814.9667, 386062.3293)

    @property
    def y_limits(self):
        return (11764.8481, 464720.9559)


class UTM(Projection):
    """
    Universal Transverse Mercator projection.

    """
    def __init__(self, zone, southern_hemisphere=False, globe=None):
        """
        Parameters
        ----------
        zone
            The numeric zone of the UTM required.
        southern_hemisphere: optional
            Set to True if the zone is in the southern hemisphere. Defaults to
            False.
        globe: optional
            An instance of :class:`cartopy.crs.Globe`. If omitted, a default
            globe is created.

        """
        proj4_params = [('proj', 'utm'),
                        ('units', 'm'),
                        ('zone', zone)]
        if southern_hemisphere:
            proj4_params.append(('south', None))
        super().__init__(proj4_params, globe=globe)
        self.threshold = 1e2

    @property
    def boundary(self):
        x0, x1 = self.x_limits
        y0, y1 = self.y_limits
        return sgeom.LinearRing([(x0, y0), (x0, y1),
                                 (x1, y1), (x1, y0),
                                 (x0, y0)])

    @property
    def x_limits(self):
        easting = 5e5
        # allow 50% overflow
        return (0 - easting/2, 2 * easting + easting/2)

    @property
    def y_limits(self):
        northing = 1e7
        # allow 50% overflow
        return (0 - northing, 2 * northing + northing/2)


class EuroPP(UTM):
    """
    UTM Zone 32 projection for EuroPP domain.

    Ellipsoid is International 1924, Datum is ED50.

    """
    def __init__(self):
        globe = Globe(ellipse='intl')
        super().__init__(32, globe=globe)

    @property
    def x_limits(self):
        return (-1.4e6, 2e6)

    @property
    def y_limits(self):
        return (4e6, 7.9e6)


class Mercator(Projection):
    """
    A Mercator projection.

    """

    def __init__(self, central_longitude=0.0,
                 min_latitude=-80.0, max_latitude=84.0,
                 globe=None, latitude_true_scale=None,
                 false_easting=0.0, false_northing=0.0, scale_factor=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to 0.
        min_latitude: optional
            The maximum southerly extent of the projection. Defaults
            to -80 degrees.
        max_latitude: optional
            The maximum northerly extent of the projection. Defaults
            to 84 degrees.
        globe: A :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.
        latitude_true_scale: optional
            The latitude where the scale is 1. Defaults to 0 degrees.
        false_easting: optional
            X offset from the planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from the planar origin in metres. Defaults to 0.
        scale_factor: optional
            Scale factor at natural origin. Defaults to unused.

        Notes
        -----
        Only one of ``latitude_true_scale`` and ``scale_factor`` should
        be included.
        """
        proj4_params = [('proj', 'merc'),
                        ('lon_0', central_longitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing),
                        ('units', 'm')]

        # If it's None, we don't pass it to Proj4, in which case its default
        # of 0.0 will be used.
        if latitude_true_scale is not None:
            proj4_params.append(('lat_ts', latitude_true_scale))

        if scale_factor is not None:
            if latitude_true_scale is not None:
                raise ValueError('It does not make sense to provide both '
                                 '"scale_factor" and "latitude_true_scale". ')
            else:
                proj4_params.append(('k_0', scale_factor))

        super().__init__(proj4_params, globe=globe)

        # Calculate limits.
        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        limits = self.transform_points(Geodetic(),
                                       np.array([minlon, maxlon]),
                                       np.array([min_latitude, max_latitude]))
        self._x_limits = tuple(limits[..., 0])
        self._y_limits = tuple(limits[..., 1])
        self.threshold = min(np.diff(self.x_limits)[0] / 720,
                             np.diff(self.y_limits)[0] / 360)

    def __eq__(self, other):
        res = super().__eq__(other)
        if hasattr(other, "_y_limits") and hasattr(other, "_x_limits"):
            res = res and self._y_limits == other._y_limits and \
                self._x_limits == other._x_limits
        return res

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.proj4_init, self._x_limits, self._y_limits))

    @property
    def boundary(self):
        x0, x1 = self.x_limits
        y0, y1 = self.y_limits
        return sgeom.LinearRing([(x0, y0), (x0, y1),
                                 (x1, y1), (x1, y0),
                                 (x0, y0)])

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


# Define a specific instance of a Mercator projection, the Google mercator.
Mercator.GOOGLE = Mercator(min_latitude=-85.0511287798066,
                           max_latitude=85.0511287798066,
                           globe=Globe(ellipse=None,
                                       semimajor_axis=WGS84_SEMIMAJOR_AXIS,
                                       semiminor_axis=WGS84_SEMIMAJOR_AXIS,
                                       nadgrids='@null'))
# Deprecated form
GOOGLE_MERCATOR = Mercator.GOOGLE


class LambertCylindrical(_RectangularProjection):
    def __init__(self, central_longitude=0.0):
        proj4_params = [('proj', 'cea'), ('lon_0', central_longitude)]
        globe = Globe(semimajor_axis=math.degrees(1))
        super().__init__(proj4_params, 180, math.degrees(1), globe=globe)


class LambertConformal(Projection):
    """
    A Lambert Conformal conic projection.

    """

    def __init__(self, central_longitude=-96.0, central_latitude=39.0,
                 false_easting=0.0, false_northing=0.0,
                 secant_latitudes=None, standard_parallels=None,
                 globe=None, cutoff=-30):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to -96.
        central_latitude: optional
            The central latitude. Defaults to 39.
        false_easting: optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from planar origin in metres. Defaults to 0.
        secant_latitudes: optional
            Secant latitudes. This keyword is deprecated in v0.12 and directly
            replaced by ``standard parallels``. Defaults to None.
        standard_parallels: optional
            Standard parallel latitude(s). Defaults to (33, 45).
        globe: optional
            A :class:`cartopy.crs.Globe`. If omitted, a default globe is
            created.
        cutoff: optional
            Latitude of map cutoff.
            The map extends to infinity opposite the central pole
            so we must cut off the map drawing before then.
            A value of 0 will draw half the globe. Defaults to -30.

        """
        proj4_params = [('proj', 'lcc'),
                        ('lon_0', central_longitude),
                        ('lat_0', central_latitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing)]
        if secant_latitudes and standard_parallels:
            raise TypeError('standard_parallels replaces secant_latitudes.')
        elif secant_latitudes is not None:
            warnings.warn('secant_latitudes has been deprecated in v0.12. '
                          'The standard_parallels keyword can be used as a '
                          'direct replacement.',
                          DeprecationWarning,
                          stacklevel=2)
            standard_parallels = secant_latitudes
        elif standard_parallels is None:
            # The default. Put this as a keyword arg default once
            # secant_latitudes is removed completely.
            standard_parallels = (33, 45)

        n_parallels = len(standard_parallels)

        if not 1 <= n_parallels <= 2:
            raise ValueError('1 or 2 standard parallels must be specified. '
                             'Got {} ({})'.format(n_parallels,
                                                  standard_parallels))

        proj4_params.append(('lat_1', standard_parallels[0]))
        if n_parallels == 2:
            proj4_params.append(('lat_2', standard_parallels[1]))

        super().__init__(proj4_params, globe=globe)

        # Compute whether this projection is at the "north pole" or the
        # "south pole" (after the central lon/lat have been taken into
        # account).
        if n_parallels == 1:
            plat = 90 if standard_parallels[0] > 0 else -90
        else:
            # Which pole are the parallels closest to? That is the direction
            # that the cone converges.
            if abs(standard_parallels[0]) > abs(standard_parallels[1]):
                poliest_sec = standard_parallels[0]
            else:
                poliest_sec = standard_parallels[1]
            plat = 90 if poliest_sec > 0 else -90

        self.cutoff = cutoff
        n = 91
        lons = np.empty(n + 2)
        lats = np.full(n + 2, float(cutoff))
        lons[0] = lons[-1] = 0
        lats[0] = lats[-1] = plat
        if plat == 90:
            # Ensure clockwise
            lons[1:-1] = np.linspace(central_longitude + 180 - 0.001,
                                     central_longitude - 180 + 0.001, n)
        else:
            lons[1:-1] = np.linspace(central_longitude - 180 + 0.001,
                                     central_longitude + 180 - 0.001, n)

        points = self.transform_points(PlateCarree(), lons, lats)

        self._boundary = sgeom.LinearRing(points)
        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

        self.threshold = 1e5

    def __eq__(self, other):
        res = super().__eq__(other)
        if hasattr(other, "cutoff"):
            res = res and self.cutoff == other.cutoff
        return res

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.proj4_init, self.cutoff))

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class LambertAzimuthalEqualArea(Projection):
    """
    A Lambert Azimuthal Equal-Area projection.

    """

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 globe=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to 0.
        central_latitude: optional
            The central latitude. Defaults to 0.
        false_easting: optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: optional
            A :class:`cartopy.crs.Globe`. If omitted, a default globe is
            created.

        """
        proj4_params = [('proj', 'laea'),
                        ('lon_0', central_longitude),
                        ('lat_0', central_latitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing)]

        super().__init__(proj4_params, globe=globe)

        a = float(self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)

        # Find the antipode, and shift it a small amount in latitude to
        # approximate the extent of the projection:
        lon = central_longitude + 180
        sign = np.sign(central_latitude) or 1
        lat = -central_latitude + sign * 0.01
        x, max_y = self.transform_point(lon, lat, PlateCarree())

        coords = _ellipse_boundary(a * 1.9999, max_y - false_northing,
                                   false_easting, false_northing, 61)
        self._boundary = sgeom.polygon.LinearRing(coords.T)
        mins = np.min(coords, axis=1)
        maxs = np.max(coords, axis=1)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]
        self.threshold = np.diff(self._x_limits)[0] * 1e-3

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class Miller(_RectangularProjection):
    _handles_ellipses = False

    def __init__(self, central_longitude=0.0, globe=None):
        if globe is None:
            globe = Globe(semimajor_axis=math.degrees(1), ellipse=None)

        # TODO: Let the globe return the semimajor axis always.
        a = float(globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)

        proj4_params = [('proj', 'mill'), ('lon_0', central_longitude)]
        # See Snyder, 1987. Eqs (11-1) and (11-2) substituting maximums of
        # (lambda-lambda0)=180 and phi=90 to get limits.
        super().__init__(proj4_params, a * np.pi, a * 2.303412543376391,
                         globe=globe)


class RotatedPole(_CylindricalProjection):
    """
    A rotated latitude/longitude projected coordinate system
    with cylindrical topology and projected distance.

    Coordinates are measured in projection metres.

    The class uses proj to perform an ob_tran operation, using the
    pole_longitude to set a lon_0 then performing two rotations based on
    pole_latitude and central_rotated_longitude.
    This is equivalent to setting the new pole to a location defined by
    the pole_latitude and pole_longitude values in the GeogCRS defined by
    globe, then rotating this new CRS about it's pole using the
    central_rotated_longitude value.

    """

    def __init__(self, pole_longitude=0.0, pole_latitude=90.0,
                 central_rotated_longitude=0.0, globe=None):
        """
        Parameters
        ----------
        pole_longitude: optional
            Pole longitude position, in unrotated degrees. Defaults to 0.
        pole_latitude: optional
            Pole latitude position, in unrotated degrees. Defaults to 0.
        central_rotated_longitude: optional
            Longitude rotation about the new pole, in degrees. Defaults to 0.
        globe: optional
            An optional :class:`cartopy.crs.Globe`. Defaults to a "WGS84"
            datum.

        """

        proj4_params = [('proj', 'ob_tran'), ('o_proj', 'latlon'),
                        ('o_lon_p', central_rotated_longitude),
                        ('o_lat_p', pole_latitude),
                        ('lon_0', 180 + pole_longitude),
                        ('to_meter', math.radians(1))]
        super().__init__(proj4_params, 180, 90, globe=globe)


class Gnomonic(Projection):
    _handles_ellipses = False

    def __init__(self, central_latitude=0.0,
                 central_longitude=0.0, globe=None):
        proj4_params = [('proj', 'gnom'), ('lat_0', central_latitude),
                        ('lon_0', central_longitude)]
        super().__init__(proj4_params, globe=globe)
        self._max = 5e7
        self.threshold = 1e5

    @property
    def boundary(self):
        return sgeom.Point(0, 0).buffer(self._max).exterior

    @property
    def x_limits(self):
        return (-self._max, self._max)

    @property
    def y_limits(self):
        return (-self._max, self._max)


class Stereographic(Projection):
    def __init__(self, central_latitude=0.0, central_longitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 true_scale_latitude=None,
                 scale_factor=None, globe=None):
        # Warn when using Stereographic with proj < 5.0.0 due to
        # incorrect transformation with lon_0=0 (see
        # https://github.com/OSGeo/proj.4/issues/194).
        if central_latitude == 0:
            if PROJ4_VERSION != ():
                if PROJ4_VERSION < (5, 0, 0):
                    warnings.warn(
                        'The Stereographic projection in Proj older than '
                        '5.0.0 incorrectly transforms points when '
                        'central_latitude=0. Use this projection with '
                        'caution.',
                        stacklevel=2)
            else:
                warnings.warn(
                    'Cannot determine Proj version. The Stereographic '
                    'projection may be unreliable and should be used with '
                    'caution.',
                    stacklevel=2)

        proj4_params = [('proj', 'stere'), ('lat_0', central_latitude),
                        ('lon_0', central_longitude),
                        ('x_0', false_easting), ('y_0', false_northing)]

        if true_scale_latitude is not None:
            if central_latitude not in (-90., 90.):
                warnings.warn('"true_scale_latitude" parameter is only used '
                              'for polar stereographic projections. Consider '
                              'the use of "scale_factor" instead.',
                              stacklevel=2)
            proj4_params.append(('lat_ts', true_scale_latitude))

        if scale_factor is not None:
            if true_scale_latitude is not None:
                raise ValueError('It does not make sense to provide both '
                                 '"scale_factor" and "true_scale_latitude". '
                                 'Ignoring "scale_factor".')
            else:
                proj4_params.append(('k_0', scale_factor))

        super().__init__(proj4_params, globe=globe)

        # TODO: Let the globe return the semimajor axis always.
        a = float(self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)
        b = float(self.globe.semiminor_axis or WGS84_SEMIMINOR_AXIS)

        # Note: The magic number has been picked to maintain consistent
        # behaviour with a wgs84 globe. There is no guarantee that the scaling
        # should even be linear.
        x_axis_offset = 5e7 / WGS84_SEMIMAJOR_AXIS
        y_axis_offset = 5e7 / WGS84_SEMIMINOR_AXIS
        self._x_limits = (-a * x_axis_offset + false_easting,
                          a * x_axis_offset + false_easting)
        self._y_limits = (-b * y_axis_offset + false_northing,
                          b * y_axis_offset + false_northing)
        coords = _ellipse_boundary(self._x_limits[1], self._y_limits[1],
                                   false_easting, false_northing, 91)
        self._boundary = sgeom.LinearRing(coords.T)
        self.threshold = np.diff(self._x_limits)[0] * 1e-3

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class NorthPolarStereo(Stereographic):
    def __init__(self, central_longitude=0.0, true_scale_latitude=None,
                 globe=None):
        super().__init__(
            central_latitude=90,
            central_longitude=central_longitude,
            true_scale_latitude=true_scale_latitude,  # None is +90
            globe=globe)


class SouthPolarStereo(Stereographic):
    def __init__(self, central_longitude=0.0, true_scale_latitude=None,
                 globe=None):
        super().__init__(
            central_latitude=-90,
            central_longitude=central_longitude,
            true_scale_latitude=true_scale_latitude,  # None is -90
            globe=globe)


class Orthographic(Projection):
    _handles_ellipses = False

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 globe=None):
        if PROJ4_VERSION != ():
            if (5, 0, 0) <= PROJ4_VERSION < (5, 1, 0):
                warnings.warn(
                    'The Orthographic projection in the v5.0.x series of Proj '
                    'incorrectly transforms points. Use this projection with '
                    'caution.',
                    stacklevel=2)
        else:
            warnings.warn(
                'Cannot determine Proj version. The Orthographic projection '
                'may be unreliable and should be used with caution.',
                stacklevel=2)

        proj4_params = [('proj', 'ortho'), ('lon_0', central_longitude),
                        ('lat_0', central_latitude)]
        super().__init__(proj4_params, globe=globe)

        # TODO: Let the globe return the semimajor axis always.
        a = float(self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)

        # To stabilise the projection of geometries, we reduce the boundary by
        # a tiny fraction at the cost of the extreme edges.
        coords = _ellipse_boundary(a * 0.99999, a * 0.99999, n=61)
        self._boundary = sgeom.polygon.LinearRing(coords.T)
        mins = np.min(coords, axis=1)
        maxs = np.max(coords, axis=1)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]
        self.threshold = np.diff(self._x_limits)[0] * 0.02

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class _WarpedRectangularProjection(Projection, metaclass=ABCMeta):
    def __init__(self, proj4_params, central_longitude,
                 false_easting=None, false_northing=None, globe=None):
        if false_easting is not None:
            proj4_params += [('x_0', false_easting)]
        if false_northing is not None:
            proj4_params += [('y_0', false_northing)]
        super().__init__(proj4_params, globe=globe)

        # Obtain boundary points
        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        n = 91
        lon = np.empty(2 * n + 1)
        lat = np.empty(2 * n + 1)
        lon[:n] = minlon
        lat[:n] = np.linspace(-90, 90, n)
        lon[n:2 * n] = maxlon
        lat[n:2 * n] = np.linspace(90, -90, n)
        lon[-1] = minlon
        lat[-1] = -90
        points = self.transform_points(self.as_geodetic(), lon, lat)

        self._boundary = sgeom.LinearRing(points)

        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class _Eckert(_WarpedRectangularProjection, metaclass=ABCMeta):
    """
    An Eckert projection.

    This class implements all the methods common to the Eckert family of
    projections.

    """

    _handles_ellipses = False

    def __init__(self, central_longitude=0, false_easting=None,
                 false_northing=None, globe=None):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        false_easting: float, optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: float, optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.

            .. note::
                This projection does not handle elliptical globes.

        """
        proj4_params = [('proj', self._proj_name),
                        ('lon_0', central_longitude)]
        super().__init__(proj4_params, central_longitude,
                         false_easting=false_easting,
                         false_northing=false_northing,
                         globe=globe)
        self.threshold = 1e5


class EckertI(_Eckert):
    """
    An Eckert I projection.

    This projection is pseudocylindrical, but not equal-area. Both meridians
    and parallels are straight lines. Its equal-area pair is :class:`EckertII`.

    """
    _proj_name = 'eck1'


class EckertII(_Eckert):
    """
    An Eckert II projection.

    This projection is pseudocylindrical, and equal-area. Both meridians and
    parallels are straight lines. Its non-equal-area pair with equally-spaced
    parallels is :class:`EckertI`.

    """
    _proj_name = 'eck2'


class EckertIII(_Eckert):
    """
    An Eckert III projection.

    This projection is pseudocylindrical, but not equal-area. Parallels are
    equally-spaced straight lines, while meridians are elliptical arcs up to
    semicircles on the edges. Its equal-area pair is :class:`EckertIV`.

    """
    _proj_name = 'eck3'


class EckertIV(_Eckert):
    """
    An Eckert IV projection.

    This projection is pseudocylindrical, and equal-area. Parallels are
    unequally-spaced straight lines, while meridians are elliptical arcs up to
    semicircles on the edges. Its non-equal-area pair with equally-spaced
    parallels is :class:`EckertIII`.

    It is commonly used for world maps.

    """
    _proj_name = 'eck4'


class EckertV(_Eckert):
    """
    An Eckert V projection.

    This projection is pseudocylindrical, but not equal-area. Parallels are
    equally-spaced straight lines, while meridians are sinusoidal arcs. Its
    equal-area pair is :class:`EckertVI`.

    """
    _proj_name = 'eck5'


class EckertVI(_Eckert):
    """
    An Eckert VI projection.

    This projection is pseudocylindrical, and equal-area. Parallels are
    unequally-spaced straight lines, while meridians are sinusoidal arcs. Its
    non-equal-area pair with equally-spaced parallels is :class:`EckertV`.

    It is commonly used for world maps.

    """
    _proj_name = 'eck6'


class EqualEarth(_WarpedRectangularProjection):
    """
    An Equal Earth projection.

    This projection is pseudocylindrical, and equal area. Parallels are
    unequally-spaced straight lines, while meridians are equally-spaced arcs.

    It is intended for world maps.

    Note
    ----
    To use this projection, you must be using Proj 5.2.0 or newer.

    References
    ----------
    Bojan \u0160avri\u010d, Tom Patterson & Bernhard Jenny (2018) The Equal
    Earth map projection, International Journal of Geographical Information
    Science, DOI: 10.1080/13658816.2018.1504949

    """

    def __init__(self, central_longitude=0, false_easting=None,
                 false_northing=None, globe=None):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        false_easting: float, optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: float, optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.

        """
        if PROJ4_VERSION < (5, 2, 0):
            raise ValueError('The EqualEarth projection requires Proj version '
                             '5.2.0, but you are using {}.'
                             .format('.'.join(str(v) for v in PROJ4_VERSION)))

        proj_params = [('proj', 'eqearth'), ('lon_0', central_longitude)]
        super().__init__(proj_params, central_longitude,
                         false_easting=false_easting,
                         false_northing=false_northing,
                         globe=globe)
        self.threshold = 1e5


class Mollweide(_WarpedRectangularProjection):
    """
    A Mollweide projection.

    This projection is pseudocylindrical, and equal area. Parallels are
    unequally-spaced straight lines, while meridians are elliptical arcs up to
    semicircles on the edges. Poles are points.

    It is commonly used for world maps, or interrupted with several central
    meridians.

    """

    _handles_ellipses = False

    def __init__(self, central_longitude=0, globe=None,
                 false_easting=None, false_northing=None):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        false_easting: float, optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: float, optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.

            .. note::
                This projection does not handle elliptical globes.

        """
        proj4_params = [('proj', 'moll'), ('lon_0', central_longitude)]
        super().__init__(proj4_params, central_longitude,
                         false_easting=false_easting,
                         false_northing=false_northing,
                         globe=globe)
        self.threshold = 1e5


class Robinson(_WarpedRectangularProjection):
    """
    A Robinson projection.

    This projection is pseudocylindrical, and a compromise that is neither
    equal-area nor conformal. Parallels are unequally-spaced straight lines,
    and meridians are curved lines of no particular form.

    It is commonly used for "visually-appealing" world maps.

    """

    _handles_ellipses = False

    def __init__(self, central_longitude=0, globe=None,
                 false_easting=None, false_northing=None):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        false_easting: float, optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: float, optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.

            .. note::
                This projection does not handle elliptical globes.

        """
        # Warn when using Robinson with proj 4.8 due to discontinuity at
        # 40 deg N introduced by incomplete fix to issue #113 (see
        # https://github.com/OSGeo/proj.4/issues/113).
        if PROJ4_VERSION != ():
            if (4, 8) <= PROJ4_VERSION < (4, 9):
                warnings.warn('The Robinson projection in the v4.8.x series '
                              'of Proj contains a discontinuity at '
                              '40 deg latitude. Use this projection with '
                              'caution.',
                              stacklevel=2)
        else:
            warnings.warn('Cannot determine Proj version. The Robinson '
                          'projection may be unreliable and should be used '
                          'with caution.',
                          stacklevel=2)

        proj4_params = [('proj', 'robin'), ('lon_0', central_longitude)]
        super().__init__(proj4_params, central_longitude,
                         false_easting=false_easting,
                         false_northing=false_northing,
                         globe=globe)
        self.threshold = 1e4

    def transform_point(self, x, y, src_crs):
        """
        Capture and handle any input NaNs, else invoke parent function,
        :meth:`_WarpedRectangularProjection.transform_point`.

        Needed because input NaNs can trigger a fatal error in the underlying
        implementation of the Robinson projection.

        Note
        ----
            Although the original can in fact translate (nan, lat) into
            (nan, y-value), this patched version doesn't support that.

        """
        if np.isnan(x) or np.isnan(y):
            result = (np.nan, np.nan)
        else:
            result = super().transform_point(x, y, src_crs)
        return result

    def transform_points(self, src_crs, x, y, z=None):
        """
        Capture and handle NaNs in input points -- else as parent function,
        :meth:`_WarpedRectangularProjection.transform_points`.

        Needed because input NaNs can trigger a fatal error in the underlying
        implementation of the Robinson projection.

        Note
        ----
            Although the original can in fact translate (nan, lat) into
            (nan, y-value), this patched version doesn't support that.
            Instead, we invalidate any of the points that contain a NaN.

        """
        input_point_nans = np.isnan(x) | np.isnan(y)
        if z is not None:
            input_point_nans |= np.isnan(z)
        handle_nans = np.any(input_point_nans)
        if handle_nans:
            # Remove NaN points from input data to avoid the error.
            x[input_point_nans] = 0.0
            y[input_point_nans] = 0.0
            if z is not None:
                z[input_point_nans] = 0.0
        result = super().transform_points(src_crs, x, y, z)
        if handle_nans:
            # Result always has shape (N, 3).
            # Blank out each (whole) point where we had a NaN in the input.
            result[input_point_nans] = np.nan
        return result


class InterruptedGoodeHomolosine(Projection):
    """
    Composite equal-area projection empahsizing either land or
    ocean features.

    Original Reference:
    Goode, J. P., 1925: The Homolosine Projection: A new device for
        portraying the Earth's surface entire. Annals of the
        Association of American Geographers, 15:3, 119-125,
        DOI: 10.1080/00045602509356949

    A central_longitude value of -160 is recommended for the oceanic view.

    """
    def __init__(self, central_longitude=0, globe=None, emphasis='land'):
        """
        Parameters
        ----------
        central_longitude : float, optional
            The central longitude, by default 0
        globe : :class:`cartopy.crs.Globe`, optional
            If omitted, a default Globe object is created, by default None
        emphasis : str, optional
            Options 'land' and 'ocean' are available, by default 'land'
        """

        if emphasis == 'land':
            proj4_params = [('proj', 'igh'), ('lon_0', central_longitude)]
            super().__init__(proj4_params, globe=globe)

        elif emphasis == 'ocean':
            if PROJ4_VERSION < (7, 1, 0):
                _proj_ver = '.'.join(str(v) for v in PROJ4_VERSION)
                raise ValueError('The Interrupted Goode Homolosine ocean '
                                 'projection requires Proj version 7.1.0, '
                                 'but you are using ' + _proj_ver)
            proj4_params = [('proj', 'igh_o'), ('lon_0', central_longitude)]
            super().__init__(proj4_params, globe=globe)

        else:
            msg = '`emphasis` needs to be either \'land\' or \'ocean\''
            raise ValueError(msg)

        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        epsilon = 1e-10

        # Obtain boundary points
        n = 31
        if emphasis == 'land':
            top_interrupted_lons = (-40.0,)
            bottom_interrupted_lons = (80.0, -20.0, -100.0)
        elif emphasis == 'ocean':
            top_interrupted_lons = (-90.0, 60.0)
            bottom_interrupted_lons = (90.0, -60.0)
        lons = np.empty(
            (2 + 2 * len(top_interrupted_lons + bottom_interrupted_lons)) * n +
            1)
        lats = np.empty(
            (2 + 2 * len(top_interrupted_lons + bottom_interrupted_lons)) * n +
            1)
        end = 0

        # Left boundary
        lons[end:end + n] = minlon
        lats[end:end + n] = np.linspace(-90, 90, n)
        end += n

        # Top boundary
        for lon in top_interrupted_lons:
            lons[end:end + n] = lon - epsilon + central_longitude
            lats[end:end + n] = np.linspace(90, 0, n)
            end += n
            lons[end:end + n] = lon + epsilon + central_longitude
            lats[end:end + n] = np.linspace(0, 90, n)
            end += n

        # Right boundary
        lons[end:end + n] = maxlon
        lats[end:end + n] = np.linspace(90, -90, n)
        end += n

        # Bottom boundary
        for lon in bottom_interrupted_lons:
            lons[end:end + n] = lon + epsilon + central_longitude
            lats[end:end + n] = np.linspace(-90, 0, n)
            end += n
            lons[end:end + n] = lon - epsilon + central_longitude
            lats[end:end + n] = np.linspace(0, -90, n)
            end += n

        # Close loop
        lons[-1] = minlon
        lats[-1] = -90

        points = self.transform_points(self.as_geodetic(), lons, lats)
        self._boundary = sgeom.LinearRing(points)

        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

        self.threshold = 2e4

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class _Satellite(Projection):
    def __init__(self, projection, satellite_height=35785831,
                 central_longitude=0.0, central_latitude=0.0,
                 false_easting=0, false_northing=0, globe=None,
                 sweep_axis=None):
        proj4_params = [('proj', projection), ('lon_0', central_longitude),
                        ('lat_0', central_latitude), ('h', satellite_height),
                        ('x_0', false_easting), ('y_0', false_northing),
                        ('units', 'm')]
        if sweep_axis:
            proj4_params.append(('sweep', sweep_axis))
        super().__init__(proj4_params, globe=globe)

    def _set_boundary(self, coords):
        self._boundary = sgeom.LinearRing(coords.T)
        mins = np.min(coords, axis=1)
        maxs = np.max(coords, axis=1)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]
        self.threshold = np.diff(self._x_limits)[0] * 0.02

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class Geostationary(_Satellite):
    """
    A view appropriate for satellites in Geostationary Earth orbit.

    Perspective view looking directly down from above a point on the equator.

    In this projection, the projected coordinates are scanning angles measured
    from the satellite looking directly downward, multiplied by the height of
    the satellite.

    """
    def __init__(self, central_longitude=0.0, satellite_height=35785831,
                 false_easting=0, false_northing=0, globe=None,
                 sweep_axis='y'):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        satellite_height: float, optional
            The height of the satellite. Defaults to 35785831 meters
            (true geostationary orbit).
        false_easting:
            X offset from planar origin in metres. Defaults to 0.
        false_northing:
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.
        sweep_axis: 'x' or 'y', optional. Defaults to 'y'.
            Controls which axis is scanned first, and thus which angle is
            applied first. The default is appropriate for Meteosat, while
            'x' should be used for GOES.
        """

        super().__init__(
            projection='geos',
            satellite_height=satellite_height,
            central_longitude=central_longitude,
            central_latitude=0.0,
            false_easting=false_easting,
            false_northing=false_northing,
            globe=globe,
            sweep_axis=sweep_axis)

        # TODO: Let the globe return the semimajor axis always.
        a = float(self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)
        h = float(satellite_height)

        # These are only exact for a spherical Earth, owing to assuming a is
        # constant. Handling elliptical would be much harder for this.
        sin_max_th = a / (a + h)
        tan_max_th = a / np.sqrt((a + h) ** 2 - a ** 2)

        # Using Napier's rules for right spherical triangles
        # See R2 and R6 (x and y coords are h * b and h * a, respectively):
        # https://en.wikipedia.org/wiki/Spherical_trigonometry
        t = np.linspace(0, -2 * np.pi, 61)  # Clockwise boundary.
        coords = np.vstack([np.arctan(tan_max_th * np.cos(t)),
                            np.arcsin(sin_max_th * np.sin(t))])
        coords *= h
        coords += np.array([[false_easting], [false_northing]])
        self._set_boundary(coords)


class NearsidePerspective(_Satellite):
    """
    Perspective view looking directly down from above a point on the globe.

    In this projection, the projected coordinates are x and y measured from
    the origin of a plane tangent to the Earth directly below the perspective
    point (e.g. a satellite).

    """

    _handles_ellipses = False

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 satellite_height=35785831,
                 false_easting=0, false_northing=0, globe=None):
        """
        Parameters
        ----------
        central_longitude: float, optional
            The central longitude. Defaults to 0.
        central_latitude: float, optional
            The central latitude. Defaults to 0.
        satellite_height: float, optional
            The height of the satellite. Defaults to 35785831 meters
            (true geostationary orbit).
        false_easting:
            X offset from planar origin in metres. Defaults to 0.
        false_northing:
            Y offset from planar origin in metres. Defaults to 0.
        globe: :class:`cartopy.crs.Globe`, optional
            If omitted, a default globe is created.

            .. note::
                This projection does not handle elliptical globes.

        """
        super().__init__(
            projection='nsper',
            satellite_height=satellite_height,
            central_longitude=central_longitude,
            central_latitude=central_latitude,
            false_easting=false_easting,
            false_northing=false_northing,
            globe=globe)

        # TODO: Let the globe return the semimajor axis always.
        a = self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS

        h = float(satellite_height)
        max_x = a * np.sqrt(h / (2 * a + h))
        coords = _ellipse_boundary(max_x, max_x,
                                   false_easting, false_northing, 61)
        self._set_boundary(coords)


class AlbersEqualArea(Projection):
    """
    An Albers Equal Area projection

    This projection is conic and equal-area, and is commonly used for maps of
    the conterminous United States.

    """

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 standard_parallels=(20.0, 50.0), globe=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to 0.
        central_latitude: optional
            The central latitude. Defaults to 0.
        false_easting: optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from planar origin in metres. Defaults to 0.
        standard_parallels: optional
            The one or two latitudes of correct scale. Defaults to (20, 50).
        globe: optional
            A :class:`cartopy.crs.Globe`. If omitted, a default globe is
            created.

        """
        proj4_params = [('proj', 'aea'),
                        ('lon_0', central_longitude),
                        ('lat_0', central_latitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing)]
        if standard_parallels is not None:
            try:
                proj4_params.append(('lat_1', standard_parallels[0]))
                try:
                    proj4_params.append(('lat_2', standard_parallels[1]))
                except IndexError:
                    pass
            except TypeError:
                proj4_params.append(('lat_1', standard_parallels))

        super().__init__(proj4_params, globe=globe)

        # bounds
        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        n = 103
        lons = np.empty(2 * n + 1)
        lats = np.empty(2 * n + 1)
        tmp = np.linspace(minlon, maxlon, n)
        lons[:n] = tmp
        lats[:n] = 90
        lons[n:-1] = tmp[::-1]
        lats[n:-1] = -90
        lons[-1] = lons[0]
        lats[-1] = lats[0]

        points = self.transform_points(self.as_geodetic(), lons, lats)

        self._boundary = sgeom.LinearRing(points)
        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

        self.threshold = 1e5

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class AzimuthalEquidistant(Projection):
    """
    An Azimuthal Equidistant projection

    This projection provides accurate angles about and distances through the
    central position. Other angles, distances, or areas may be distorted.
    """

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 globe=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The true longitude of the central meridian in degrees.
            Defaults to 0.
        central_latitude: optional
            The true latitude of the planar origin in degrees.
            Defaults to 0.
        false_easting: optional
            X offset from the planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from the planar origin in metres. Defaults to 0.
        globe: optional
            An instance of :class:`cartopy.crs.Globe`. If omitted, a default
            globe is created.

        """
        # Warn when using Azimuthal Equidistant with proj < 4.9.2 due to
        # incorrect transformation past 90 deg distance (see
        # https://github.com/OSGeo/proj.4/issues/246).
        if PROJ4_VERSION != ():
            if PROJ4_VERSION < (4, 9, 2):
                warnings.warn('The Azimuthal Equidistant projection in Proj '
                              'older than 4.9.2 incorrectly transforms points '
                              'farther than 90 deg from the origin. Use this '
                              'projection with caution.',
                              stacklevel=2)
        else:
            warnings.warn('Cannot determine Proj version. The Azimuthal '
                          'Equidistant projection may be unreliable and '
                          'should be used with caution.',
                          stacklevel=2)

        proj4_params = [('proj', 'aeqd'), ('lon_0', central_longitude),
                        ('lat_0', central_latitude),
                        ('x_0', false_easting), ('y_0', false_northing)]
        super().__init__(proj4_params, globe=globe)

        # TODO: Let the globe return the semimajor axis always.
        a = float(self.globe.semimajor_axis or WGS84_SEMIMAJOR_AXIS)
        b = float(self.globe.semiminor_axis or a)

        coords = _ellipse_boundary(a * np.pi, b * np.pi,
                                   false_easting, false_northing, 61)
        self._boundary = sgeom.LinearRing(coords.T)
        mins = np.min(coords, axis=1)
        maxs = np.max(coords, axis=1)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

        self.threshold = 1e5

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class Sinusoidal(Projection):
    """
    A Sinusoidal projection.

    This projection is equal-area.

    """

    def __init__(self, central_longitude=0.0, false_easting=0.0,
                 false_northing=0.0, globe=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to 0.
        false_easting: optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from planar origin in metres. Defaults to 0.
        globe: optional
            A :class:`cartopy.crs.Globe`. If omitted, a default globe is
            created.

        """
        proj4_params = [('proj', 'sinu'),
                        ('lon_0', central_longitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing)]
        super().__init__(proj4_params, globe=globe)

        # Obtain boundary points
        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        points = []
        n = 91
        lon = np.empty(2 * n + 1)
        lat = np.empty(2 * n + 1)
        lon[:n] = minlon
        lat[:n] = np.linspace(-90, 90, n)
        lon[n:2 * n] = maxlon
        lat[n:2 * n] = np.linspace(90, -90, n)
        lon[-1] = minlon
        lat[-1] = -90
        points = self.transform_points(self.as_geodetic(), lon, lat)

        self._boundary = sgeom.LinearRing(points)
        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]
        self.threshold = max(np.abs(self.x_limits + self.y_limits)) * 1e-5

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


# MODIS data products use a Sinusoidal projection of a spherical Earth
# https://modis-land.gsfc.nasa.gov/GCTP.html
Sinusoidal.MODIS = Sinusoidal(globe=Globe(ellipse=None,
                                          semimajor_axis=6371007.181,
                                          semiminor_axis=6371007.181))


class EquidistantConic(Projection):
    """
    An Equidistant Conic projection.

    This projection is conic and equidistant, and the scale is true along all
    meridians and along one or two specified standard parallels.
    """

    def __init__(self, central_longitude=0.0, central_latitude=0.0,
                 false_easting=0.0, false_northing=0.0,
                 standard_parallels=(20.0, 50.0), globe=None):
        """
        Parameters
        ----------
        central_longitude: optional
            The central longitude. Defaults to 0.
        central_latitude: optional
            The true latitude of the planar origin in degrees. Defaults to 0.
        false_easting: optional
            X offset from planar origin in metres. Defaults to 0.
        false_northing: optional
            Y offset from planar origin in metres. Defaults to 0.
        standard_parallels: optional
            The one or two latitudes of correct scale. Defaults to (20, 50).
        globe: optional
            A :class:`cartopy.crs.Globe`. If omitted, a default globe is
            created.

        """
        proj4_params = [('proj', 'eqdc'),
                        ('lon_0', central_longitude),
                        ('lat_0', central_latitude),
                        ('x_0', false_easting),
                        ('y_0', false_northing)]
        if standard_parallels is not None:
            try:
                proj4_params.append(('lat_1', standard_parallels[0]))
                try:
                    proj4_params.append(('lat_2', standard_parallels[1]))
                except IndexError:
                    pass
            except TypeError:
                proj4_params.append(('lat_1', standard_parallels))

        super().__init__(proj4_params, globe=globe)

        # bounds
        n = 103
        lons = np.empty(2 * n + 1)
        lats = np.empty(2 * n + 1)
        minlon, maxlon = self._determine_longitude_bounds(central_longitude)
        tmp = np.linspace(minlon, maxlon, n)
        lons[:n] = tmp
        lats[:n] = 90
        lons[n:-1] = tmp[::-1]
        lats[n:-1] = -90
        lons[-1] = lons[0]
        lats[-1] = lats[0]

        points = self.transform_points(self.as_geodetic(), lons, lats)

        self._boundary = sgeom.LinearRing(points)
        mins = np.min(points, axis=0)
        maxs = np.max(points, axis=0)
        self._x_limits = mins[0], maxs[0]
        self._y_limits = mins[1], maxs[1]

        self.threshold = 1e5

    @property
    def boundary(self):
        return self._boundary

    @property
    def x_limits(self):
        return self._x_limits

    @property
    def y_limits(self):
        return self._y_limits


class _BoundaryPoint:
    def __init__(self, distance, kind, data):
        """
        A representation for a geometric object which is
        connected to the boundary.

        Parameters
        ----------
        distance: float
            The distance along the boundary that this object
            can be found.
        kind: bool
            Whether this object represents a point from the
            pre-computed boundary.
        data: point or namedtuple
            The actual data that this boundary object represents.

        """
        self.distance = distance
        self.kind = kind
        self.data = data

    def __repr__(self):
        return '_BoundaryPoint({!r}, {!r}, {})'.format(
            self.distance, self.kind, self.data
        )


def _find_first_ge(a, x):
    for v in a:
        if v.distance >= x:
            return v
    # We've gone all the way around, so pick the first point again.
    return a[0]


def epsg(code):
    """
    Return the projection which corresponds to the given EPSG code.

    The EPSG code must correspond to a "projected coordinate system",
    so EPSG codes such as 4326 (WGS-84) which define a "geodetic coordinate
    system" will not work.

    Note
    ----
        The conversion is performed by querying https://epsg.io/ so a
        live internet connection is required.

    """
    import cartopy._epsg
    return cartopy._epsg._EPSGProjection(code)
