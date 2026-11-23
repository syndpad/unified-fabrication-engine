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
    Incorporates v12.0 MARBLE_AUTOLIST options: target layer & hatch material mapping,
    grid seed sampling & outer wrapper filtering, COS margin & detail-sheet classification,
    minimum-wastage ABB rotation, 4-sided dimensions, and Excel/DXF deliverables.
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
        self.seed_probe_points = []
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

    def inspect_hatch_keys(self) -> list:
        """Return unique (PATTERN_NAME, COLOR_INDEX) keys found on target/modelspace hatches."""
        if self.msp is None:
            self.load_dxf()
        keys = set()
        for entity in self._collect_geometry_entities():
            if entity.dxftype() == "HATCH":
                pat = (getattr(entity.dxf, "pattern_name", "") or "SOLID").upper()
                col = int(getattr(entity.dxf, "color", 256) or 256)
                keys.add((pat, col))
        return sorted(keys)

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

    @staticmethod
    def _point_in_polygon(pt: tuple, poly: list) -> bool:
        """Ray-casting test for whether a point lies inside a closed polygon."""
        x, y = pt[0], pt[1]
        inside = False
        n = len(poly)
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i][0], poly[i][1]
            xj, yj = poly[j][0], poly[j][1]
            denom = (yj - yi)
            if abs(denom) < 1e-12:
                denom = 1e-12
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / denom + xi):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _cluster_coords(values: list, tol: float = 15.0) -> list:
        """Cluster sorted coordinate values within tolerance (v12 seed grid helper)."""
        if not values:
            return []
        clustered = [float(values[0])]
        for val in values[1:]:
            val_f = float(val)
            if val_f - clustered[-1] > tol:
                clustered.append(val_f)
        return clustered

    def _build_grid_seed_points(self, segments: list, cluster_tol: float = 15.0) -> list:
        """Build interior cell seed points from X/Y coordinate grid and angled line offsets."""
        raw_xs = []
        raw_ys = []
        diag_probes = []

        for seg in segments:
            for pt in seg:
                raw_xs.append(pt[0])
                raw_ys.append(pt[1])
            if len(seg) >= 2:
                p1, p2 = seg[0], seg[-1]
                dx, dy = p2[0] - p1[0], p2[1] - p1[1]
                seg_len = math.hypot(dx, dy)
                if seg_len > 20.0 and abs(dx) > 5.0 and abs(dy) > 5.0:
                    mx, my = (p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5
                    nx, ny = -dy / seg_len, dx / seg_len
                    diag_probes.append((round(mx + nx * 45.0, 3), round(my + ny * 45.0, 3)))
                    diag_probes.append((round(mx - nx * 45.0, 3), round(my - ny * 45.0, 3)))

        ux = self._cluster_coords(sorted(raw_xs), tol=cluster_tol)
        uy = self._cluster_coords(sorted(raw_ys), tol=cluster_tol)

        probes = list(diag_probes)
        for i in range(len(ux) - 1):
            x_a, x_b = ux[i], ux[i + 1]
            if (x_b - x_a) > 25.0:
                cx = (x_a + x_b) * 0.5
                for j in range(len(uy) - 1):
                    y_a, y_b = uy[j], uy[j + 1]
                    if (y_b - y_a) > 25.0:
                        cy = (y_a + y_b) * 0.5
                        probes.append((round(cx, 3), round(cy, 3)))

        return probes

    def _get_min_wastage_bbox_and_angle(self, points: list) -> tuple:
        """
        Calculate minimum-area bounding box (length, width) and optimal rotation angle
        across candidate orientations (v11/v12 ABB rotation logic).
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

    def _extract_closed_loops_from_lines_and_arcs(self, tolerance: float = 2.0) -> list:
        """
        Extract individual closed loops from separate LINE, ARC, and SPLINE segments
        on the selected layer, prioritizing smaller closed panels over large outer envelopes.
        """
        segments = []
        for entity in self._collect_geometry_entities():
            if entity.dxftype() in ("LINE", "ARC", "SPLINE"):
                try:
                    p = make_path(entity)
                    pts = [(round(v.x, 3), round(v.y, 3)) for v in p.flattening(distance=0.5)]
                    cleaned = self._clean_polygon_points(pts, eps=1e-3)
                    if len(cleaned) >= 2:
                        seg_len = sum(
                            math.hypot(cleaned[i][0] - cleaned[i - 1][0], cleaned[i][1] - cleaned[i - 1][1])
                            for i in range(1, len(cleaned))
                        )
                        if seg_len > 1e-2:
                            segments.append((cleaned, seg_len))
                except Exception:
                    continue

        self.seed_probe_points = self._build_grid_seed_points([s[0] for s in segments])

        segments.sort(key=lambda item: item[1])
        seg_polys = [item[0] for item in segments]

        def dist(p1, p2):
            return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

        closed_loops = []
        used_segments = set()

        for start_idx in range(len(seg_polys)):
            if start_idx in used_segments:
                continue

            chain = list(seg_polys[start_idx])
            chain_used = {start_idx}
            closed_here = False

            for _ in range(len(seg_polys)):
                if len(chain) >= 3 and dist(chain[-1], chain[0]) <= tolerance:
                    closed_here = True
                    break

                best_match = None
                best_dist = tolerance + 1e-6

                for cand_idx, seg in enumerate(seg_polys):
                    if cand_idx in chain_used:
                        continue

                    d_tail_start = dist(chain[-1], seg[0])
                    if d_tail_start <= best_dist:
                        best_dist = d_tail_start
                        best_match = ("tail", cand_idx, seg[1:])

                    d_tail_end = dist(chain[-1], seg[-1])
                    if d_tail_end <= best_dist:
                        best_dist = d_tail_end
                        best_match = ("tail", cand_idx, list(reversed(seg[:-1])))

                    d_head_end = dist(chain[0], seg[-1])
                    if d_head_end <= best_dist:
                        best_dist = d_head_end
                        best_match = ("head", cand_idx, seg[:-1])

                    d_head_start = dist(chain[0], seg[0])
                    if d_head_start <= best_dist:
                        best_dist = d_head_start
                        best_match = ("head", cand_idx, list(reversed(seg[1:])))

                if best_match is None:
                    break

                side, cand_idx, piece_pts = best_match
                if side == "tail":
                    chain.extend(piece_pts)
                else:
                    chain = piece_pts + chain

                chain_used.add(cand_idx)

                if len(chain) >= 3 and dist(chain[-1], chain[0]) <= tolerance:
                    closed_here = True
                    break

            if closed_here:
                cleaned = self._clean_polygon_points(chain, eps=1e-3)
                if len(cleaned) >= 3 and self._polygon_area(cleaned) >= self.min_piece_area_mm2:
                    closed_loops.append(cleaned)
                    used_segments.update(chain_used)

        return closed_loops

    def _deduplicate_polygons(self, records: list, pos_tol: float = 2.0, area_tol: float = 0.03) -> list:
        """Remove near-duplicate closed polygons representing the same panel."""
        unique = []
        for rec in records:
            pts = rec["points"]
            area = abs(self._polygon_area(pts))
            cx, cy = (
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts)
            )
            is_dup = False
            for existing in unique:
                e_pts = existing["points"]
                e_area = abs(self._polygon_area(e_pts))
                e_cx, e_cy = (
                    sum(p[0] for p in e_pts) / len(e_pts),
                    sum(p[1] for p in e_pts) / len(e_pts)
                )
                if math.hypot(cx - e_cx, cy - e_cy) <= pos_tol and abs(area - e_area) / max(area, e_area, 1e-6) <= area_tol:
                    is_dup = True
                    break
            if not is_dup:
                unique.append(rec)
        return unique

    @staticmethod
    def _filter_outer_frame_if_needed(records: list) -> list:
        """Remove a single enclosing outer border if it wraps multiple smaller panels."""
        if len(records) < 3:
            return records

        entries = []
        for rec in records:
            pts = rec["points"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            bbox_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 0.0)
            entries.append((rec, bbox, bbox_area))

        max_entry = max(entries, key=lambda x: x[2])
        _, max_box, max_area = max_entry

        enclosed_count = 0
        for item in entries:
            if item is max_entry:
                continue
            _, b, _ = item
            if (
                b[0] >= max_box[0] - 1.0
                and b[1] >= max_box[1] - 1.0
                and b[2] <= max_box[2] + 1.0
                and b[3] <= max_box[3] + 1.0
            ):
                enclosed_count += 1

        if enclosed_count >= 2 and max_area > 0:
            return [rec for rec, _, _ in entries if rec is not max_entry[0]]

        return records

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
        Extract closed panels on the target layer, classify special detail sheets vs.
        perimeter COS panels vs. standard panels, assign 3-digit tags (001, 002, ...),
        and apply kerf + COS allowances.
        """
        if self.msp is None:
            self.load_dxf()

        kerf = float(self.profile["default_kerf_mm"])
        raw_records = []

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

        for entity in self._collect_geometry_entities():
            if entity.dxftype() in ("CIRCLE", "ELLIPSE"):
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

        closed_loops = self._extract_closed_loops_from_lines_and_arcs(tolerance=self.snap_tolerance)
        for poly in closed_loops:
            raw_records.append({
                "source": "Closed Line/Arc Panel",
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
