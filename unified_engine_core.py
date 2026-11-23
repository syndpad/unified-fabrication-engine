import os
import math
import ezdxf
from ezdxf.path import make_path
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


class UnifiedFabricationEngine:
    """
    Unified Multi-Material Fabrication Core for Architectural Contracting
    Supports Marble & Granite, MDF & Wood Joinery, Architectural Metals, and Aluminum Profiles.
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
        exclude_outer_frame: bool = False,
        snap_tolerance: float = 3.0
    ):
        self.dxf_path = dxf_path
        self.material_key = material_key if material_key in self.DEFAULT_PROFILES else "marble_granite"
        self.profile = self.DEFAULT_PROFILES[self.material_key].copy()
        self.target_layer = target_layer.strip() if isinstance(target_layer, str) and target_layer.strip() else None
        self.exclude_outer_frame = exclude_outer_frame
        self.snap_tolerance = float(snap_tolerance)

        if custom_kerf is not None:
            self.profile["default_kerf_mm"] = float(custom_kerf)

        self.doc = None
        self.msp = None
        self.extracted_pieces = []
        self.summary_metrics = {}

    def load_dxf(self):
        """Read DXF file and initialize modelspace."""
        if not os.path.exists(self.dxf_path):
            raise FileNotFoundError(f"DXF file not found: {self.dxf_path}")

        self.doc = ezdxf.readfile(self.dxf_path)
        self.msp = self.doc.modelspace()

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

    def inspect_dxf_entities(self) -> dict:
        """Return counts of entity types present in modelspace (filtered by target_layer if set)."""
        if self.msp is None:
            self.load_dxf()
        counts = {}
        for entity in self._collect_geometry_entities():
            t = entity.dxftype()
            counts[t] = counts.get(t, 0) + 1
        return counts

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

    def _classify_shape(self, points: list) -> str:
        """Classify closed detailed parts: Rectangle/Square, Triangle, Curved/Arc, or Custom Polygon."""
        pts = self._clean_polygon_points(points)
        if len(pts) < 3:
            return "Custom / Detail"

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bbox_w = max(xs) - min(xs)
        bbox_h = max(ys) - min(ys)
        bbox_area = bbox_w * bbox_h
        poly_area = self._polygon_area(pts)

        if len(pts) == 3:
            return "Triangle"

        if len(pts) >= 12 and bbox_area > 0:
            area_ratio = poly_area / bbox_area
            if abs(area_ratio - (math.pi / 4.0)) < 0.08 or len(pts) >= 18:
                return "Curved / Arc"

        if bbox_area > 0 and len(pts) <= 6:
            if abs(bbox_area - poly_area) / bbox_area <= 0.02:
                return "Standard Rect"

        return "Custom / Detail"

    def _get_min_wastage_bbox(self, points: list) -> tuple:
        """Calculate minimum-area bounding box across candidate orientations."""
        pts = self._clean_polygon_points(points)
        if len(pts) < 3:
            return 0.0, 0.0

        min_area = float("inf")
        best_w, best_h = 0.0, 0.0

        for angle_deg in range(0, 90, 5):
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

        return round(best_w, 2), round(best_h, 2)

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

    def _layer_matches(self, entity) -> bool:
        """Return True if no target layer is set or entity layer matches target_layer."""
        if not self.target_layer:
            return True
        ent_layer = getattr(entity.dxf, "layer", "")
        return ent_layer.strip().lower() == self.target_layer.lower()

    def _collect_geometry_entities(self):
        """Yield entities matching target_layer from modelspace and exploded INSERT blocks."""
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

    def _merge_loose_lines_and_arcs(self, tolerance: float = 3.0) -> list:
        """
        Build complete closed loops from LINE, ARC, and SPLINE segments on the
        selected material layer using bi-directional endpoint snapping.
        """
        segments = []
        for entity in self._collect_geometry_entities():
            if entity.dxftype() in ("LINE", "ARC", "SPLINE"):
                try:
                    p = make_path(entity)
                    pts = [(round(v.x, 3), round(v.y, 3)) for v in p.flattening(distance=0.5)]
                    pts = self._clean_polygon_points(pts, eps=1e-3)
                    if len(pts) >= 2:
                        segments.append(pts)
                except Exception:
                    continue

        def dist(p1, p2):
            return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

        closed_loops = []
        used_segments = set()

        for start_idx in range(len(segments)):
            if start_idx in used_segments:
                continue

            chain = list(segments[start_idx])
            chain_used = {start_idx}
            closed_here = False

            while True:
                if len(chain) >= 3 and dist(chain[-1], chain[0]) <= tolerance:
                    closed_here = True
                    break

                best_match = None
                best_dist = tolerance + 1e-6

                for cand_idx, seg in enumerate(segments):
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

            cleaned = self._clean_polygon_points(chain, eps=1e-3)
            if closed_here and len(cleaned) >= 3 and self._polygon_area(cleaned) > 0.5:
                closed_loops.append(cleaned)
                used_segments.update(chain_used)

        return closed_loops

    @staticmethod
    def _filter_outer_frame_if_needed(polygons_with_source: list) -> list:
        """
        If an oversized outer border encloses several smaller pieces, remove the
        enclosing outer frame so detailed inner parts remain cleanly isolated.
        """
        if len(polygons_with_source) < 3:
            return polygons_with_source

        areas_and_boxes = []
        for src, pts in polygons_with_source:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            bbox_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 0.0)
            areas_and_boxes.append((src, pts, bbox, bbox_area))

        max_entry = max(areas_and_boxes, key=lambda x: x[3])
        _, _, max_box, max_area = max_entry

        enclosed_count = 0
        for item in areas_and_boxes:
            if item is max_entry:
                continue
            _, _, b, _ = item
            if (
                b[0] >= max_box[0] - 1.0
                and b[1] >= max_box[1] - 1.0
                and b[2] <= max_box[2] + 1.0
                and b[3] <= max_box[3] + 1.0
            ):
                enclosed_count += 1

        if enclosed_count >= 2 and max_area > 0:
            return [
                (src, pts)
                for src, pts, _, _ in areas_and_boxes
                if not (src == max_entry[0] and pts == max_entry[1])
            ]

        return polygons_with_source

    def process_geometry(self) -> list:
        """
        Extract complete closed polylines, curves, and detailed LINE/ARC/SPLINE loops
        for the target material layer, assign 3-digit tags (001, 002, ...),
        and apply cutting kerf allowances.
        """
        if self.msp is None:
            self.load_dxf()

        kerf = float(self.profile["default_kerf_mm"])
        raw_polygons = []

        for entity in self._collect_geometry_entities():
            etype = entity.dxftype()
            if etype in ("LWPOLYLINE", "POLYLINE"):
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                except Exception:
                    if etype == "LWPOLYLINE":
                        pts = list(entity.get_points(format="xy"))
                    else:
                        pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]

                cleaned_pts = self._clean_polygon_points(pts)
                is_closed_flag = bool(getattr(entity, "closed", False) or getattr(entity, "is_closed", False))
                near_closed = (
                    len(cleaned_pts) >= 3
                    and math.hypot(cleaned_pts[0][0] - cleaned_pts[-1][0], cleaned_pts[0][1] - cleaned_pts[-1][1]) <= self.snap_tolerance
                )
                if len(cleaned_pts) >= 3 and (is_closed_flag or near_closed) and self._polygon_area(cleaned_pts) > 0.5:
                    raw_polygons.append(("Polyline Part", cleaned_pts))

        for entity in self._collect_geometry_entities():
            if entity.dxftype() in ("CIRCLE", "ELLIPSE"):
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                    cleaned_pts = self._clean_polygon_points(pts)
                    if len(cleaned_pts) >= 6 and self._polygon_area(cleaned_pts) > 0.5:
                        raw_polygons.append(("Curved Loop", cleaned_pts))
                except Exception:
                    continue

        merged_loops = self._merge_loose_lines_and_arcs(tolerance=self.snap_tolerance)
        for pts in merged_loops:
            cleaned_pts = self._clean_polygon_points(pts)
            if len(cleaned_pts) >= 3 and self._polygon_area(cleaned_pts) > 0.5:
                raw_polygons.append(("Detailed Line/Arc", cleaned_pts))

        if self.exclude_outer_frame:
            raw_polygons = self._filter_outer_frame_if_needed(raw_polygons)

        layer_label = self.target_layer if self.target_layer else "All Layers"
        self.extracted_pieces = []
        for idx, (source_type, pts) in enumerate(raw_polygons, start=1):
            net_len, net_wid = self._get_min_wastage_bbox(pts)
            net_area_m2 = self._polygon_area(pts) / 1_000_000.0

            cut_len = round(net_len + kerf, 2)
            cut_wid = round(net_wid + kerf, 2)
            gross_bbox_area_m2 = (cut_len * cut_wid) / 1_000_000.0
            shape_type = self._classify_shape(pts)

            if net_len > 0 and net_wid > 0:
                self.extracted_pieces.append({
                    "Tag": f"{idx:03d}",
                    "Material": self.profile["name"],
                    "Target Layer": layer_label,
                    "Source Entity": source_type,
                    "Shape Type": shape_type,
                    "Net Length (mm)": net_len,
                    "Net Width (mm)": net_wid,
                    "Cut Length + Kerf (mm)": cut_len,
                    "Cut Width + Kerf (mm)": cut_wid,
                    "Net Area (m2)": round(net_area_m2, 4),
                    "Gross BBox Area (m2)": round(gross_bbox_area_m2, 4),
                    "Polygon Points": pts
                })

        return self.extracted_pieces

    def calculate_sheets_and_wastage(self) -> dict:
        """Estimate required sheets (2D) or linear bars (1D) and overall wastage percentage."""
        if not self.extracted_pieces:
            self.process_geometry()

        total_pieces = len(self.extracted_pieces)
        detail_pieces = sum(1 for p in self.extracted_pieces if p.get("Shape Type") != "Standard Rect")
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
                "Material Profile": self.profile["name"],
                "Target Layer": layer_label,
                "Total Pieces": total_pieces,
                "Detail Sheet Pieces": detail_pieces,
                "Standard Sheet Size (mm)": f"{int(sheet_l)} x {int(sheet_w)}",
                "Single Sheet Area (m2)": round(single_sheet_area_m2, 3),
                "Total Net Area (m2)": round(total_net_area, 3),
                "Total BBox Area + Kerf (m2)": round(total_gross_bbox_area, 3),
                "Estimated Sheets Required": estimated_sheets,
                "Total Raw Material Area (m2)": round(total_purchased_area, 3),
                "Estimated Wastage (%)": round(wastage_pct, 2)
            }
        else:
            bar_len = float(self.profile["standard_bar_mm"])
            total_linear_mm = sum(p["Cut Length + Kerf (mm)"] for p in self.extracted_pieces)
            estimated_bars = (
                math.ceil((total_linear_mm * float(self.profile["wastage_factor"])) / bar_len)
                if total_pieces > 0 and bar_len > 0
                else 0
            )
            total_purchased_mm = estimated_bars * bar_len
            wastage_pct = ((total_purchased_mm - total_linear_mm) / total_purchased_mm * 100.0) if total_purchased_mm > 0 else 0.0

            self.summary_metrics = {
                "Material Profile": self.profile["name"],
                "Target Layer": layer_label,
                "Total Pieces": total_pieces,
                "Detail Sheet Pieces": detail_pieces,
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
        Save a clean DXF shop drawing containing closed polylines and
        3-digit tags (001, 002, ...) without circles in dedicated layers.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        out_doc = ezdxf.new(dxfversion="R2010")
        if self.doc is not None and "$INSUNITS" in self.doc.header:
            out_doc.header["$INSUNITS"] = self.doc.header["$INSUNITS"]

        out_msp = out_doc.modelspace()

        if "GAFY_CUT_POLYLINES" not in out_doc.layers:
            out_doc.layers.add("GAFY_CUT_POLYLINES", color=3)
        if "GAFY_PART_TAGS" not in out_doc.layers:
            out_doc.layers.add("GAFY_PART_TAGS", color=2)

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

            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            w = float(piece.get("Net Width (mm)", 100.0))
            text_height = max(min(w * 0.12, 60.0), 15.0)

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
            raise ValueError("No valid closed polygons available to export to DXF.")

        out_doc.saveas(output_dxf_path)
        return output_dxf_path

    def export_detail_sheets_dxf(
        self,
        output_dxf_path: str = "Detail_Sheets.dxf",
        sheet_width: float = 1189.0,
        sheet_height: float = 841.0,
        cols_per_sheet: int = 2,
        rows_per_sheet: int = 2,
        include_all_if_no_custom: bool = True
    ) -> str:
        """
        Generate dedicated Detail Sheets in DXF for detailed/custom/non-rectangular
        pieces with frame borders, 3-digit tags (no circles), and overall dimensions.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        target_pieces = [
            p for p in self.extracted_pieces
            if p.get("Shape Type") != "Standard Rect"
        ]
        if not target_pieces and include_all_if_no_custom:
            target_pieces = list(self.extracted_pieces)

        if not target_pieces:
            raise ValueError("No pieces available for Detail Sheets export.")

        out_doc = ezdxf.new(dxfversion="R2010")
        if self.doc is not None and "$INSUNITS" in self.doc.header:
            out_doc.header["$INSUNITS"] = self.doc.header["$INSUNITS"]

        out_msp = out_doc.modelspace()

        for layer_name, color_idx in [
            ("GAFY_SHEET_FRAME", 7),
            ("GAFY_DETAIL_GEOMETRY", 3),
            ("GAFY_DETAIL_TAGS", 2),
            ("GAFY_DETAIL_DIMS", 4)
        ]:
            if layer_name not in out_doc.layers:
                out_doc.layers.add(layer_name, color=color_idx)

        pieces_per_sheet = max(cols_per_sheet * rows_per_sheet, 1)
        sheet_spacing_x = sheet_width + 250.0
        cell_w = (sheet_width - 120.0) / cols_per_sheet
        cell_h = (sheet_height - 160.0) / rows_per_sheet

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
                    f"DETAIL SHEET {sheet_idx + 1:02d} | {self.profile['name']} | {piece.get('Target Layer', 'All Layers')}",
                    dxfattribs={
                        "layer": "GAFY_DETAIL_DIMS",
                        "height": 22.0,
                        "insert": (origin_x + 40.0, origin_y + sheet_height - 45.0)
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

            cell_origin_x = origin_x + 60.0 + c_idx * cell_w
            cell_origin_y = origin_y + sheet_height - 100.0 - (r_idx + 1) * cell_h

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

            out_msp.add_text(
                f"TAG {piece['Tag']} | {piece['Shape Type']} | L:{piece['Net Length (mm)']} x W:{piece['Net Width (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_TAGS",
                    "height": 14.0,
                    "insert": (cell_origin_x + 15.0, cell_origin_y + 12.0)
                }
            )

        out_doc.saveas(output_dxf_path)
        return output_dxf_path
