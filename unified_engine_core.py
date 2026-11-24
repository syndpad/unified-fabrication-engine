import os
import math
import ezdxf
from ezdxf import recover
from ezdxf.path import make_path
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


class UnifiedFabricationEngine:
    """
    Unified Multi-Material Fabrication Core for Architectural Contracting.
    Splits intersecting/crossing lines and curves in 2D to extract individual
    closed tile/panel boundaries on the target layer, avoiding outer borders.
    """

    DEFAULT_PROFILES = {
        "marble_granite": {
            "name": "Marble & Granite",
            "mode": "plate_2d",
            "default_kerf_mm": 4.0,
            "sheet_length_mm": 2800.0,
            "sheet_width_mm": 1600.0,
            "wastage_factor": 1.12,
            "notes": "Saw kerf, slab nesting, and polishing allowance applied"
        },
        "mdf_wood": {
            "name": "MDF & Wood Joinery",
            "mode": "plate_2d",
            "default_kerf_mm": 3.2,
            "sheet_length_mm": 2440.0,
            "sheet_width_mm": 1220.0,
            "wastage_factor": 1.08,
            "notes": "Panel saw kerf and edge/grain allowance applied"
        },
        "stainless_steel": {
            "name": "Stainless Steel / Metals",
            "mode": "plate_2d",
            "default_kerf_mm": 1.5,
            "sheet_length_mm": 3000.0,
            "sheet_width_mm": 1500.0,
            "wastage_factor": 1.05,
            "notes": "Laser/plasma kerf and forming clearance applied"
        },
        "aluminum_profile": {
            "name": "Aluminum Profiles",
            "mode": "linear_1d",
            "default_kerf_mm": 5.0,
            "standard_bar_mm": 6000.0,
            "wastage_factor": 1.06,
            "notes": "1D linear extrusion bar cut optimization"
        }
    }

    def __init__(
        self,
        dxf_path: str,
        material_key: str = "marble_granite",
        custom_kerf: float = None,
        target_layer: str | None = None,
        exclude_outer_frame: bool = True,
        snap_tolerance: float = 2.0,
        min_piece_area_mm2: float = 25.0,
        max_chain_steps: int = 24,
        outer_area_ratio_threshold: float = 1.65,
        thickness_mm: int = 20,
        cos_margin_mm: float = 50.0,
        perimeter_band_mm: float = 80.0,
        background_material: str = "Burdur Beige Polished",
        hatch_material_map: dict | None = None,
        list_no: str = "D20899",
        project_name: str = "Architectural Contracting Project"
    ):
        self.dxf_path = dxf_path
        self.material_key = material_key if material_key in self.DEFAULT_PROFILES else "marble_granite"
        self.profile = self.DEFAULT_PROFILES[self.material_key].copy()
        self.target_layer = target_layer.strip() if isinstance(target_layer, str) and target_layer.strip() else None
        self.exclude_outer_frame = bool(exclude_outer_frame)
        self.snap_tolerance = float(snap_tolerance)
        self.min_piece_area_mm2 = float(min_piece_area_mm2)
        self.max_chain_steps = max(4, int(max_chain_steps))
        self.outer_area_ratio_threshold = float(outer_area_ratio_threshold)
        self.thickness_mm = int(thickness_mm)
        self.cos_margin_mm = float(cos_margin_mm)
        self.perimeter_band_mm = float(perimeter_band_mm)
        self.background_material = background_material.strip() if background_material else "Burdur Beige Polished"
        self.hatch_material_map = hatch_material_map or {}
        self.list_no = list_no
        self.project_name = project_name

        if custom_kerf is not None:
            self.profile["default_kerf_mm"] = float(custom_kerf)

        self.doc = None
        self.msp = None
        self.extracted_pieces = []
        self.summary_metrics = {}

    def load_dxf(self):
        """Read DXF file with automatic recovery fallback if standard read fails."""
        if not self.dxf_path or not os.path.exists(self.dxf_path):
            raise FileNotFoundError(f"The file '{self.dxf_path}' could not be found.")

        try:
            self.doc = ezdxf.readfile(self.dxf_path)
            self.msp = self.doc.modelspace()
        except Exception as primary_err:
            try:
                doc, _ = recover.readfile(self.dxf_path)
                if doc is None:
                    raise RuntimeError(f"The file '{self.dxf_path}' could not be opened: {primary_err}")
                self.doc = doc
                self.msp = self.doc.modelspace()
            except Exception as recover_err:
                raise RuntimeError(
                    f"The file '{self.dxf_path}' could not be opened. "
                    f"Standard read error: {primary_err} | Recovery error: {recover_err}"
                )

    def get_available_layers(self) -> list:
        """Return sorted list of layer names present in the DXF file."""
        if self.msp is None:
            self.load_dxf()

        layers = set()
        for layer in self.doc.layers:
            if layer.dxf.name:
                layers.add(layer.dxf.name)
        for entity in self.msp:
            l_name = getattr(entity.dxf, "layer", None)
            if l_name:
                layers.add(l_name)
        return sorted(layers)

    def _layer_matches(self, entity) -> bool:
        """Return True if no target layer is set or entity layer matches target_layer."""
        if not self.target_layer:
            return True
        ent_layer = getattr(entity.dxf, "layer", "")
        return ent_layer.strip().lower() == self.target_layer.lower()

    def _collect_geometry_entities(self):
        """Yield entities matching target_layer from modelspace and virtual INSERT entities."""
        for entity in self.msp:
            if self._layer_matches(entity):
                yield entity
            if entity.dxftype() == "INSERT":
                try:
                    for virtual_ent in entity.virtual_entities():
                        if self._layer_matches(virtual_ent) or self._layer_matches(entity):
                            yield virtual_ent
                except Exception:
                    continue

    @staticmethod
    def _clean_polygon_points(points: list, eps: float = 1e-3) -> list:
        """Remove invalid coordinates and consecutive duplicate points."""
        cleaned = []
        for p in points:
            x, y = float(p[0]), float(p[1])
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            if not cleaned or math.hypot(x - cleaned[-1][0], y - cleaned[-1][1]) > eps:
                cleaned.append((x, y))
        if len(cleaned) > 1 and math.hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1]) <= eps:
            cleaned.pop()
        return cleaned

    def _polygon_area(self, points: list) -> float:
        """Compute polygon area in square millimeters using the shoelace formula."""
        pts = self._clean_polygon_points(points)
        n = len(pts)
        if n < 3:
            return 0.0
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += pts[i][0] * pts[j][1]
            area -= pts[j][0] * pts[i][1]
        return abs(area) / 2.0

    def _polygon_centroid(self, points: list) -> tuple:
        """Return (cx, cy) centroid of a closed polygon."""
        pts = self._clean_polygon_points(points)
        if not pts:
            return (0.0, 0.0)
        return (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts)
        )

    def _get_min_wastage_bbox_and_angle(self, points: list) -> tuple:
        """
        Calculate minimum-area bounding box (length, width) and optimal rotation angle
        across candidate orientations.
        """
        pts = self._clean_polygon_points(points)
        if len(pts) < 3:
            return 0.0, 0.0, 0.0

        min_area = float("inf")
        best_w, best_h, best_angle = 0.0, 0.0, 0.0

        candidate_angles = [float(a) for a in range(0, 90, 5)]
        n = len(pts)
        for i in range(n):
            p1, p2 = pts[i], pts[(i + 1) % n]
            ang_deg = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0])) % 90.0
            candidate_angles.append(ang_deg)

        for angle_deg in candidate_angles:
            rad = math.radians(angle_deg)
            cos_a, sin_a = math.cos(rad), math.sin(rad)

            rot_x = [p[0] * cos_a - p[1] * sin_a for p in pts]
            rot_y = [p[0] * sin_a + p[1] * cos_a for p in pts]

            w = max(rot_x) - min(rot_x)
            h = max(rot_y) - min(rot_y)
            area = w * h

            if area > 0 and area < min_area:
                min_area = area
                best_w, best_h = max(w, h), min(w, h)
                best_angle = round(angle_deg, 2)

        return round(best_w, 2), round(best_h, 2), best_angle

    @staticmethod
    def _segment_intersection_2d(a1: tuple, a2: tuple, b1: tuple, b2: tuple, eps: float = 1e-4) -> tuple | None:
        """Return intersection point (x, y) of 2D segments a1-a2 and b1-b2 if they cross/touch."""
        x1, y1 = a1
        x2, y2 = a2
        x3, y3 = b1
        x4, y4 = b2

        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-12:
            return None

        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

        if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
            ix = x1 + max(0.0, min(1.0, t)) * (x2 - x1)
            iy = y1 + max(0.0, min(1.0, t)) * (y2 - y1)
            return (round(ix, 3), round(iy, 3))
        return None

    def _split_intersecting_polylines(self, polylines: list) -> list:
        """
        Split polylines/segments at all 2D intersection points with other segments
        so crossing lines and curves break cleanly at tile boundaries.
        """
        sub_segs = []
        for poly in polylines:
            for i in range(len(poly) - 1):
                p1, p2 = poly[i], poly[i + 1]
                if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) > 1e-2:
                    sub_segs.append([p1, p2])

        n_segs = len(sub_segs)
        split_pts_per_seg = [[s[0], s[1]] for s in sub_segs]

        for i in range(n_segs):
            a1, a2 = sub_segs[i][0], sub_segs[i][1]
            ax0, ax1 = min(a1[0], a2[0]), max(a1[0], a2[0])
            ay0, ay1 = min(a1[1], a2[1]), max(a1[1], a2[1])

            for j in range(i + 1, n_segs):
                b1, b2 = sub_segs[j][0], sub_segs[j][1]
                bx0, bx1 = min(b1[0], b2[0]), max(b1[0], b2[0])
                by0, by1 = min(b1[1], b2[1]), max(b1[1], b2[1])

                if ax1 < bx0 - 1.0 or bx1 < ax0 - 1.0 or ay1 < by0 - 1.0 or by1 < ay0 - 1.0:
                    continue

                inter = self._segment_intersection_2d(a1, a2, b1, b2)
                if inter is not None:
                    split_pts_per_seg[i].append(inter)
                    split_pts_per_seg[j].append(inter)

        atomic_edges = []
        for idx, pts_list in enumerate(split_pts_per_seg):
            p_start, p_end = sub_segs[idx][0], sub_segs[idx][1]
            dx, dy = p_end[0] - p_start[0], p_end[1] - p_start[1]
            ordered = sorted(pts_list, key=lambda p: (p[0] - p_start[0]) * dx + (p[1] - p_start[1]) * dy)
            cleaned = self._clean_polygon_points(ordered, eps=max(self.snap_tolerance * 0.25, 0.25))
            for k in range(len(cleaned) - 1):
                if math.hypot(cleaned[k + 1][0] - cleaned[k][0], cleaned[k + 1][1] - cleaned[k][1]) > 0.5:
                    atomic_edges.append((cleaned[k], cleaned[k + 1]))

        return atomic_edges

    def _extract_minimal_faces_from_intersections(self, polylines: list) -> list:
        """
        Build a planar-style graph from split crossing segments and extract small
        closed tile faces by turning toward the sharpest angle at each junction.
        """
        edges = self._split_intersecting_polylines(polylines)
        if not edges:
            return []

        # Snap vertices within tolerance
        nodes = []
        node_map = {}

        def get_node(pt: tuple) -> int:
            for idx, npt in enumerate(nodes):
                if math.hypot(pt[0] - npt[0], pt[1] - npt[1]) <= self.snap_tolerance:
                    return idx
            idx = len(nodes)
            nodes.append((round(pt[0], 3), round(pt[1], 3)))
            node_map[idx] = nodes[-1]
            return idx

        # Directed half-edges: (u, v, angle_from_u_to_v)
        adj = {}
        half_edges = []
        for p1, p2 in edges:
            u = get_node(p1)
            v = get_node(p2)
            if u == v:
                continue
            ang_uv = math.atan2(nodes[v][1] - nodes[u][1], nodes[v][0] - nodes[u][0])
            ang_vu = math.atan2(nodes[u][1] - nodes[v][1], nodes[u][0] - nodes[v][0])
            he_id_1 = len(half_edges)
            half_edges.append((u, v, ang_uv))
            adj.setdefault(u, []).append((ang_uv, v, he_id_1))

            he_id_2 = len(half_edges)
            half_edges.append((v, u, ang_vu))
            adj.setdefault(v, []).append((ang_vu, u, he_id_2))

        for u in adj:
            adj[u].sort(key=lambda item: item[0])

        visited_half_edges = set()
        candidate_faces = []

        for start_he in range(len(half_edges)):
            if start_he in visited_half_edges:
                continue

            cur_he = start_he
            u0, v0, ang_prev = half_edges[cur_he]
            face_nodes = [u0, v0]
            used_in_face = {cur_he}
            closed = False

            for _ in range(self.max_chain_steps):
                _, cur_v, cur_ang = half_edges[cur_he]
                if cur_v == u0 and len(face_nodes) >= 3:
                    closed = True
                    break

                outgoing = adj.get(cur_v, [])
                if not outgoing:
                    break

                # Reverse direction is cur_ang + pi; pick next sharpest right/left turn
                rev_ang = (cur_ang + math.pi + 2 * math.pi) % (2 * math.pi)
                best_next = None
                best_turn = float("inf")

                for ang_out, nxt_node, nxt_he_id in outgoing:
                    if nxt_node == face_nodes[-2] and len(outgoing) > 1:
                        continue
                    diff = (rev_ang - ang_out) % (2 * math.pi)
                    if diff < 1e-4:
                        diff = 2 * math.pi
                    if diff < best_turn:
                        best_turn = diff
                        best_next = (nxt_node, nxt_he_id)

                if best_next is None:
                    break

                nxt_node, nxt_he_id = best_next
                if nxt_he_id in used_in_face and nxt_node != u0:
                    break

                cur_he = nxt_he_id
                used_in_face.add(cur_he)
                face_nodes.append(nxt_node)

                if nxt_node == u0 and len(face_nodes) >= 3:
                    closed = True
                    break

            if closed:
                poly_pts = self._clean_polygon_points([nodes[nid] for nid in face_nodes[:-1]], eps=0.25)
                area = self._polygon_area(poly_pts)
                if len(poly_pts) >= 3 and area >= self.min_piece_area_mm2:
                    candidate_faces.append((area, poly_pts, used_in_face))

        # Sort smallest faces first so individual small tiles are kept before large loops
        candidate_faces.sort(key=lambda item: item[0])
        accepted_faces = []
        consumed_hes = set()

        for area, poly_pts, he_set in candidate_faces:
            overlap = len(he_set.intersection(consumed_hes))
            if overlap <= max(1, len(he_set) // 3):
                accepted_faces.append(poly_pts)
                consumed_hes.update(he_set)

        return accepted_faces

    def _deduplicate_polygons(self, records: list, pos_tol: float = 5.0, area_tol: float = 0.05) -> list:
        """Remove near-duplicate closed polygons representing the same tile."""
        unique = []
        for rec in records:
            pts = rec["points"]
            area = abs(self._polygon_area(pts))
            cx, cy = self._polygon_centroid(pts)
            is_dup = False
            for existing in unique:
                e_pts = existing["points"]
                e_area = abs(self._polygon_area(e_pts))
                e_cx, e_cy = self._polygon_centroid(e_pts)
                if math.hypot(cx - e_cx, cy - e_cy) <= pos_tol and abs(area - e_area) / max(area, e_area, 1e-6) <= area_tol:
                    is_dup = True
                    break
            if not is_dup:
                unique.append(rec)
        return unique

    def _filter_outer_frame_if_needed(self, records: list) -> list:
        """
        Remove the overall tiles-area outline boundary when smaller individual tiles are
        detected.
        """
        if len(records) < 2:
            return records

        entries = []
        for rec in records:
            pts = rec["points"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            area = abs(self._polygon_area(pts))
            entries.append((rec, bbox, area))

        areas_sorted = sorted(e[2] for e in entries if e[2] > 0)
        if len(areas_sorted) < 2:
            return records

        q25_idx = max(0, (len(areas_sorted) - 1) // 4)
        baseline_small_area = areas_sorted[q25_idx]

        filtered = []
        for rec, box, area in entries:
            enclosed_others = 0
            for other_rec, _, other_area in entries:
                if other_rec is rec or other_area >= area * 0.90:
                    continue
                ocx, ocy = self._polygon_centroid(other_rec["points"])
                if (
                    ocx >= box[0] - 1.0
                    and ocx <= box[2] + 1.0
                    and ocy >= box[1] - 1.0
                    and ocy <= box[3] + 1.0
                ):
                    enclosed_others += 1

            is_oversized_outline = (
                (enclosed_others >= 1 and area > baseline_small_area * self.outer_area_ratio_threshold)
                or (area > baseline_small_area * (self.outer_area_ratio_threshold * 1.5))
            )
            if not is_oversized_outline:
                filtered.append(rec)

        return filtered if filtered else records

    @staticmethod
    def _sort_records_by_grid(records: list, row_band: float = 150.0) -> list:
        """Sort panels top-to-bottom (row Y descending), then left-to-right (X ascending)."""
        def sort_key(rec):
            pts = rec["points"]
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            row_bucket = -round(cy / max(row_band, 1.0))
            return (row_bucket, cx)

        return sorted(records, key=sort_key)

    def _resolve_hatch_material_for_centroid(self, cx: float, cy: float) -> str:
        """Match piece centroid against hatch bounding boxes if a hatch material map is provided."""
        if not self.hatch_material_map:
            return self.background_material

        for entity in self._collect_geometry_entities():
            if entity.dxftype() == "HATCH":
                pat = (getattr(entity.dxf, "pattern_name", "") or "SOLID").upper()
                col = int(getattr(entity.dxf, "color", 256) or 256)
                mapped = self.hatch_material_map.get(f"{pat}:{col}") or self.hatch_material_map.get(pat)
                if not mapped:
                    continue
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=1.0)]
                    if len(pts) >= 3:
                        xs = [pt[0] for pt in pts]
                        ys = [pt[1] for pt in pts]
                        if min(xs) <= cx <= max(xs) and min(ys) <= cy <= max(ys):
                            return mapped
                except Exception:
                    continue

        return self.background_material

    def process_geometry(self) -> list:
        """
        Extract individual closed tile/panel boundaries formed by closed polylines,
        curves, and intersecting/crossing lines/arcs on the target layer.
        """
        if self.msp is None:
            self.load_dxf()

        kerf = float(self.profile["default_kerf_mm"])
        raw_records = []
        open_or_line_polylines = []

        for entity in self._collect_geometry_entities():
            etype = entity.dxftype()
            has_bulge = False
            if etype == "LWPOLYLINE":
                try:
                    has_bulge = any(abs(float(pt[4])) > 1e-4 for pt in entity.get_points(format="xyb"))
                except Exception:
                    has_bulge = False

            if etype in ("LWPOLYLINE", "POLYLINE"):
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                except Exception:
                    if etype == "LWPOLYLINE":
                        pts = [(pt[0], pt[1]) for pt in entity.get_points(format="xy")]
                    else:
                        pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]

                cleaned_pts = self._clean_polygon_points(pts)
                is_closed_flag = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
                near_closed = (
                    len(cleaned_pts) >= 3
                    and math.hypot(cleaned_pts[0][0] - cleaned_pts[-1][0], cleaned_pts[0][1] - cleaned_pts[-1][1]) <= self.snap_tolerance
                )
                if len(cleaned_pts) >= 3 and (is_closed_flag or near_closed) and self._polygon_area(cleaned_pts) >= self.min_piece_area_mm2:
                    raw_records.append({
                        "source": "Closed Polyline Panel",
                        "points": cleaned_pts,
                        "vertex_count": len(cleaned_pts),
                        "has_arcs": has_bulge
                    })
                elif len(cleaned_pts) >= 2:
                    open_or_line_polylines.append(cleaned_pts)

            elif etype in ("CIRCLE", "ELLIPSE"):
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                    cleaned_pts = self._clean_polygon_points(pts)
                    if len(cleaned_pts) >= 6 and self._polygon_area(cleaned_pts) >= self.min_piece_area_mm2:
                        raw_records.append({
                            "source": "Closed Curved Panel",
                            "points": cleaned_pts,
                            "vertex_count": len(cleaned_pts),
                            "has_arcs": True
                        })
                except Exception:
                    continue

            elif etype in ("LINE", "ARC", "SPLINE"):
                try:
                    p = make_path(entity)
                    pts = [(round(v.x, 3), round(v.y, 3)) for v in p.flattening(distance=0.5)]
                    cleaned = self._clean_polygon_points(pts, eps=1e-3)
                    if len(cleaned) >= 2:
                        open_or_line_polylines.append(cleaned)
                except Exception:
                    continue

        # Extract minimal closed faces from intersecting/crossing lines and curves
        intersecting_faces = self._extract_minimal_faces_from_intersections(open_or_line_polylines)
        for poly in intersecting_faces:
            raw_records.append({
                "source": "Intersecting Line/Curve Tile",
                "points": poly,
                "vertex_count": len(poly),
                "has_arcs": len(poly) >= 12
            })

        raw_records = self._deduplicate_polygons(raw_records)
        if self.exclude_outer_frame:
            raw_records = self._filter_outer_frame_if_needed(raw_records)

        raw_records = self._sort_records_by_grid(raw_records)

        if raw_records:
            all_xs = [p[0] for r in raw_records for p in r["points"]]
            all_ys = [p[1] for r in raw_records for p in r["points"]]
            all_min_x, all_max_x = min(all_xs), max(all_xs)
            all_min_y, all_max_y = min(all_ys), max(all_ys)
        else:
            all_min_x = all_max_x = all_min_y = all_max_y = 0.0

        layer_label = self.target_layer if self.target_layer else "All Layers"
        self.extracted_pieces = []
        detail_counter = 1

        for idx, rec in enumerate(raw_records, start=1):
            pts = rec["points"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            box_w = max_x - min_x
            box_h = max_y - min_y

            net_len, net_wid, abb_angle = self._get_min_wastage_bbox_and_angle(pts)
            net_area_m2 = self._polygon_area(pts) / 1_000_000.0
            rect_area = box_w * box_h
            area_diff = abs(rect_area - (net_area_m2 * 1_000_000.0))

            touch_l = min_x <= (all_min_x + self.perimeter_band_mm)
            touch_r = max_x >= (all_max_x - self.perimeter_band_mm)
            touch_b = min_y <= (all_min_y + self.perimeter_band_mm)
            touch_t = max_y >= (all_max_y - self.perimeter_band_mm)

            if rec.get("vertex_count", 4) > 4 or area_diff > 25.0 or rec.get("has_arcs", False):
                shape_type = "Special Detail"
                detail_sheet = str(detail_counter)
                remarks = "-"
                cos_add_w = 0.0
                cos_add_h = 0.0
                detail_counter += 1
            elif touch_l or touch_r or touch_b or touch_t:
                shape_type = "Perimeter COS"
                detail_sheet = "-"
                if (touch_l or touch_r) and (touch_b or touch_t):
                    remarks = "H/W-COS"
                    cos_add_w = self.cos_margin_mm
                    cos_add_h = self.cos_margin_mm
                elif touch_b or touch_t:
                    remarks = "H-COS"
                    cos_add_w = 0.0
                    cos_add_h = self.cos_margin_mm
                else:
                    remarks = "W-COS"
                    cos_add_w = self.cos_margin_mm
                    cos_add_h = 0.0
            else:
                shape_type = "Standard Rect"
                detail_sheet = "-"
                remarks = "-"
                cos_add_w = 0.0
                cos_add_h = 0.0

            cut_len = round(net_len + kerf + cos_add_w, 2)
            cut_wid = round(net_wid + kerf + cos_add_h, 2)
            gross_bbox_area_m2 = (cut_len * cut_wid) / 1_000_000.0

            cx, cy = self._polygon_centroid(pts)
            assigned_mat = self._resolve_hatch_material_for_centroid(cx, cy)

            if net_len > 0 and net_wid > 0 and net_area_m2 > 0:
                self.extracted_pieces.append({
                    "Tag": f"{idx:03d}",
                    "Material Profile": self.profile["name"],
                    "Stone / Material": assigned_mat,
                    "Target Layer": layer_label,
                    "Shape Type": shape_type,
                    "Details Sheet": detail_sheet,
                    "Remarks": remarks,
                    "Th (mm)": self.thickness_mm,
                    "Net Length (mm)": net_len,
                    "Net Width (mm)": net_wid,
                    "Cut Length + Kerf/COS (mm)": cut_len,
                    "Cut Width + Kerf/COS (mm)": cut_wid,
                    "Net Area (m2)": round(net_area_m2, 4),
                    "Gross BBox Area (m2)": round(gross_bbox_area_m2, 4),
                    "ABB Angle (deg)": abb_angle,
                    "Polygon Points": pts
                })

        return self.extracted_pieces

    def calculate_sheets_and_wastage(self) -> dict:
        """Estimate required sheets (2D) or linear bars (1D) and overall wastage percentage."""
        if not self.extracted_pieces:
            self.process_geometry()

        total_pieces = len(self.extracted_pieces)
        detail_pieces = sum(1 for p in self.extracted_pieces if p.get("Details Sheet") != "-")
        cos_pieces = sum(1 for p in self.extracted_pieces if p.get("Remarks") != "-")
        total_net_area = sum(p["Net Area (m2)"] for p in self.extracted_pieces)
        total_gross_bbox_area = sum(p["Gross BBox Area (m2)"] for p in self.extracted_pieces)
        layer_label = self.target_layer if self.target_layer else "All Layers"

        if self.profile["mode"] == "plate_2d":
            sheet_l = float(self.profile["sheet_length_mm"])
            sheet_w = float(self.profile["sheet_width_mm"])
            single_sheet_area_m2 = (sheet_l * sheet_w) / 1_000_000.0

            effective_required_area = total_gross_bbox_area * float(self.profile["wastage_factor"])
            estimated_sheets = (
                math.ceil(effective_required_area / single_sheet_area_m2)
                if total_pieces > 0 and single_sheet_area_m2 > 0
                else 0
            )

            total_purchased_area = estimated_sheets * single_sheet_area_m2
            wastage_area = max(0.0, total_purchased_area - total_net_area)
            wastage_pct = (wastage_area / total_purchased_area * 100.0) if total_purchased_area > 0 else 0.0

            self.summary_metrics = {
                "List No": self.list_no,
                "Material Profile": self.profile["name"],
                "Background Material": self.background_material,
                "Target Layer": layer_label,
                "Total Closed Panels": total_pieces,
                "Detail Sheet Pieces": detail_pieces,
                "Perimeter COS Pieces": cos_pieces,
                "Standard Sheet Size (mm)": f"{int(sheet_l)} x {int(sheet_w)}",
                "Single Sheet Area (m2)": round(single_sheet_area_m2, 3),
                "Total Net Area (m2)": round(total_net_area, 3),
                "Total BBox Area + Kerf/COS (m2)": round(total_gross_bbox_area, 3),
                "Estimated Sheets Required": estimated_sheets,
                "Total Raw Material Area (m2)": round(total_purchased_area, 3),
                "Estimated Wastage (%)": round(wastage_pct, 2)
            }
        else:
            bar_len = float(self.profile["standard_bar_mm"])
            total_linear_mm = sum(p["Cut Length + Kerf/COS (mm)"] for p in self.extracted_pieces)
            estimated_bars = (
                math.ceil((total_linear_mm * float(self.profile["wastage_factor"])) / bar_len)
                if total_pieces > 0 and bar_len > 0
                else 0
            )
            total_purchased_mm = estimated_bars * bar_len
            wastage_pct = ((total_purchased_mm - total_linear_mm) / total_purchased_mm * 100.0) if total_purchased_mm > 0 else 0.0

            self.summary_metrics = {
                "List No": self.list_no,
                "Material Profile": self.profile["name"],
                "Target Layer": layer_label,
                "Total Closed Panels": total_pieces,
                "Detail Sheet Pieces": detail_pieces,
                "Perimeter COS Pieces": cos_pieces,
                "Standard Bar Length (mm)": int(bar_len),
                "Total Cut Length (m)": round(total_linear_mm / 1000.0, 2),
                "Estimated Bars Required": estimated_bars,
                "Estimated Wastage (%)": round(wastage_pct, 2)
            }

        return self.summary_metrics

    def export_to_excel(self, output_excel_path: str = "Unified_Cutlist_Report.xlsx") -> str:
        """Export cutting list and sheets/wastage summary to a formatted Excel workbook."""
        if not self.summary_metrics:
            self.calculate_sheets_and_wastage()

        clean_rows = [
            {k: v for k, v in p.items() if k != "Polygon Points"}
            for p in self.extracted_pieces
        ]
        df_cutlist = pd.DataFrame(clean_rows)
        df_summary = pd.DataFrame(
            list(self.summary_metrics.items()),
            columns=["Parameter", "Value"]
        )

        with pd.ExcelWriter(output_excel_path, engine="openpyxl") as writer:
            df_cutlist.to_excel(writer, index=False, sheet_name="Cutting_List")
            df_summary.to_excel(writer, index=False, sheet_name="Sheets_&_Wastage_Summary")

            workbook = writer.book
            header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
            header_font = Font(color="FFFFFF", bold=True, size=11)
            thin_border = Border(
                left=Side(style="thin", color="D9D9D9"),
                right=Side(style="thin", color="D9D9D9"),
                top=Side(style="thin", color="D9D9D9"),
                bottom=Side(style="thin", color="D9D9D9")
            )

            for sheet_name in ["Cutting_List", "Sheets_&_Wastage_Summary"]:
                ws = workbook[sheet_name]
                for cell in ws[1]:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")

                for row in ws.iter_rows(min_row=2):
                    for cell in row:
                        cell.border = thin_border
                        cell.alignment = Alignment(horizontal="center", vertical="center")

                for col in ws.columns:
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws.column_dimensions[col[0].column_letter].width = max(max_len + 4, 14)

        return output_excel_path

    def export_processed_dxf(self, output_dxf_path: str = "Processed_Tagged_Drawing.dxf") -> str:
        """
        Save a clean DXF shop drawing containing closed polylines for each detected
        panel and 3-digit tags (001, 002, ...) without circles in dedicated layers.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        out_doc = ezdxf.new(dxfversion="R2010")
        if self.doc is not None and "$INSUNITS" in self.doc.header:
            out_doc.header["$INSUNITS"] = self.doc.header["$INSUNITS"]

        out_msp = out_doc.modelspace()

        for layer_name, color_idx in [
            ("GAFY_CUT_POLYLINES", 3),
            ("GAFY_PART_TAGS", 2)
        ]:
            if layer_name not in out_doc.layers:
                out_doc.layers.add(layer_name, color=color_idx)

        written_count = 0
        for piece in self.extracted_pieces:
            pts = self._clean_polygon_points(piece.get("Polygon Points", []))
            if len(pts) < 3:
                continue

            out_msp.add_lwpolyline(
                pts,
                close=True,
                dxfattribs={"layer": "GAFY_CUT_POLYLINES"}
            )

            cx, cy = self._polygon_centroid(pts)
            w = float(piece.get("Net Width (mm)", 100.0))
            h = float(piece.get("Net Length (mm)", 100.0))
            text_height = max(min(min(w, h) * 0.35, 90.0), 20.0)

            out_msp.add_text(
                str(piece["Tag"]),
                dxfattribs={
                    "layer": "GAFY_PART_TAGS",
                    "height": float(text_height),
                    "insert": (float(cx), float(cy))
                }
            )
            written_count += 1

        if written_count == 0:
            raise ValueError("No valid closed panel polylines available to export to DXF.")

        out_doc.saveas(output_dxf_path)
        return output_dxf_path

    def export_detail_sheets_dxf(
        self,
        output_dxf_path: str = "Detail_Sheets.dxf",
        sheet_width: float = 2200.0,
        sheet_height: float = 3200.0,
        cols_per_sheet: int = 2,
        rows_per_sheet: int = 1,
        include_all_if_no_custom: bool = True
    ) -> str:
        """
        Generate adaptive Detail Sheets in DXF with frame borders, minimum-wastage ABB
        bounding boxes, 4-sided overall dimensions, and 3-digit tags (no circles).
        """
        if not self.extracted_pieces:
            self.process_geometry()

        target_pieces = [
            p for p in self.extracted_pieces
            if p.get("Details Sheet") != "-"
        ]
        if not target_pieces and include_all_if_no_custom:
            target_pieces = list(self.extracted_pieces)

        if not target_pieces:
            raise ValueError("No closed panels available for Detail Sheets export.")

        out_doc = ezdxf.new(dxfversion="R2010")
        if self.doc is not None and "$INSUNITS" in self.doc.header:
            out_doc.header["$INSUNITS"] = self.doc.header["$INSUNITS"]

        out_msp = out_doc.modelspace()

        for layer_name, color_idx in [
            ("GAFY_SHEET_FRAME", 7),
            ("GAFY_DETAIL_GEOMETRY", 3),
            ("GAFY_ABB_CUTTING", 1),
            ("GAFY_DETAIL_TAGS", 2),
            ("GAFY_DETAIL_DIMS", 4)
        ]:
            if layer_name not in out_doc.layers:
                out_doc.layers.add(layer_name, color=color_idx)

        pieces_per_sheet = max(cols_per_sheet * rows_per_sheet, 1)
        sheet_spacing_x = sheet_width + 400.0
        cell_w = (sheet_width - 180.0) / cols_per_sheet
        cell_h = (sheet_height - 240.0) / rows_per_sheet

        for idx, piece in enumerate(target_pieces):
            sheet_idx = idx // pieces_per_sheet
            local_idx = idx % pieces_per_sheet
            r_idx = local_idx // cols_per_sheet
            c_idx = local_idx % cols_per_sheet

            origin_x = sheet_idx * sheet_spacing_x
            origin_y = 0.0

            if local_idx == 0:
                frame_pts = [
                    (origin_x, origin_y),
                    (origin_x + sheet_width, origin_y),
                    (origin_x + sheet_width, origin_y + sheet_height),
                    (origin_x, origin_y + sheet_height)
                ]
                out_msp.add_lwpolyline(
                    frame_pts,
                    close=True,
                    dxfattribs={"layer": "GAFY_SHEET_FRAME"}
                )
                out_msp.add_text(
                    f"DETAIL SHEET {sheet_idx + 1:02d} | {self.list_no} | {self.profile['name']} | {piece.get('Target Layer', 'All Layers')}",
                    dxfattribs={
                        "layer": "GAFY_DETAIL_DIMS",
                        "height": 30.0,
                        "insert": (origin_x + 60.0, origin_y + sheet_height - 70.0)
                    }
                )

            pts = self._clean_polygon_points(piece.get("Polygon Points", []))
            if len(pts) < 3:
                continue

            min_x = min(p[0] for p in pts)
            max_x = max(p[0] for p in pts)
            min_y = min(p[1] for p in pts)
            max_y = max(p[1] for p in pts)
            span_w = max(max_x - min_x, 1e-3)
            span_h = max(max_y - min_y, 1e-3)

            usable_w = cell_w * 0.72
            usable_h = cell_h * 0.65
            scale = min(usable_w / span_w, usable_h / span_h)

            cell_origin_x = origin_x + 90.0 + c_idx * cell_w
            cell_origin_y = origin_y + sheet_height - 150.0 - (r_idx + 1) * cell_h

            scaled_pts = [
                (
                    cell_origin_x + (cell_w - span_w * scale) * 0.5 + (p[0] - min_x) * scale,
                    cell_origin_y + (cell_h - span_h * scale) * 0.5 + (p[1] - min_y) * scale
                )
                for p in pts
            ]

            out_msp.add_lwpolyline(
                scaled_pts,
                close=True,
                dxfattribs={"layer": "GAFY_DETAIL_GEOMETRY"}
            )

            s_xs = [p[0] for p in scaled_pts]
            s_ys = [p[1] for p in scaled_pts]
            s_min_x, s_max_x = min(s_xs), max(s_xs)
            s_min_y, s_max_y = min(s_ys), max(s_ys)

            abb_pts = [
                (s_min_x, s_min_y),
                (s_max_x, s_min_y),
                (s_max_x, s_max_y),
                (s_min_x, s_max_y)
            ]
            out_msp.add_lwpolyline(
                abb_pts,
                close=True,
                dxfattribs={"layer": "GAFY_ABB_CUTTING"}
            )

            out_msp.add_text(
                f"W: {piece['Net Width (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_DIMS",
                    "height": 16.0,
                    "insert": ((s_min_x + s_max_x) * 0.5, s_max_y + 22.0)
                }
            )
            out_msp.add_text(
                f"Cut W: {piece['Cut Width + Kerf/COS (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_DIMS",
                    "height": 14.0,
                    "insert": ((s_min_x + s_max_x) * 0.5, s_min_y - 34.0)
                }
            )
            out_msp.add_text(
                f"L: {piece['Net Length (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_DIMS",
                    "height": 16.0,
                    "insert": (s_min_x - 120.0, (s_min_y + s_max_y) * 0.5)
                }
            )
            out_msp.add_text(
                f"Cut L: {piece['Cut Length + Kerf/COS (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_DIMS",
                    "height": 14.0,
                    "insert": (s_max_x + 20.0, (s_min_y + s_max_y) * 0.5)
                }
            )

            out_msp.add_text(
                f"TAG {piece['Tag']} | Sheet {piece['Details Sheet']} | {piece['Stone / Material']}",
                dxfattribs={
                    "layer": "GAFY_DETAIL_TAGS",
                    "height": 16.0,
                    "insert": (cell_origin_x + 15.0, cell_origin_y + 12.0)
                }
            )

        out_doc.saveas(output_dxf_path)
        return output_dxf_path
