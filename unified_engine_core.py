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

    def __init__(self, dxf_path: str, material_key: str = "marble_granite", custom_kerf: float = None):
        self.dxf_path = dxf_path
        self.material_key = material_key if material_key in self.DEFAULT_PROFILES else "marble_granite"
        self.profile = self.DEFAULT_PROFILES[self.material_key].copy()

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

    @staticmethod
    def _clean_polygon_points(points: list, eps: float = 1e-4) -> list:
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

    def _is_standard_rectangle(self, points: list, area_tol: float = 0.02) -> bool:
        """
        Check whether a closed polygon closely matches a simple axis-aligned rectangle.
        Non-rectangular or custom shapes return False so they receive detail sheets.
        """
        pts = self._clean_polygon_points(points)
        if len(pts) < 4:
            return False

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bbox_w = max(xs) - min(xs)
        bbox_h = max(ys) - min(ys)
        bbox_area = bbox_w * bbox_h
        if bbox_area <= 0:
            return False

        poly_area = self._polygon_area(pts)
        area_ratio_diff = abs(bbox_area - poly_area) / bbox_area
        return len(pts) <= 5 and area_ratio_diff <= area_tol

    def _get_min_wastage_bbox(self, points: list) -> tuple:
        """
        Calculate minimum-area bounding box across candidate orientations
        to reduce sheet wastage on angled or irregular parts.
        """
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

    def _merge_loose_lines_and_arcs(self, tolerance: float = 0.5) -> list:
        """
        Connect adjacent LINE and ARC entities into closed polygon point lists.
        """
        loose_paths = []
        for entity in self.msp:
            if entity.dxftype() in ("LINE", "ARC"):
                try:
                    p = make_path(entity)
                    pts = [(round(v.x, 3), round(v.y, 3)) for v in p.flattening(distance=0.5)]
                    pts = self._clean_polygon_points(pts, eps=1e-3)
                    if len(pts) >= 2:
                        loose_paths.append(pts)
                except Exception:
                    continue

        closed_polygons = []
        used = [False] * len(loose_paths)

        def dist(p1, p2):
            return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

        for i in range(len(loose_paths)):
            if used[i]:
                continue

            chain = list(loose_paths[i])
            used[i] = True
            merged = True

            while merged:
                merged = False
                for j in range(len(loose_paths)):
                    if used[j]:
                        continue
                    seg = loose_paths[j]

                    if dist(chain[-1], seg[0]) <= tolerance:
                        chain.extend(seg[1:])
                        used[j] = True
                        merged = True
                    elif dist(chain[-1], seg[-1]) <= tolerance:
                        chain.extend(reversed(seg[:-1]))
                        used[j] = True
                        merged = True
                    elif dist(chain[0], seg[-1]) <= tolerance:
                        chain = seg[:-1] + chain
                        used[j] = True
                        merged = True
                    elif dist(chain[0], seg[0]) <= tolerance:
                        chain = list(reversed(seg[1:])) + chain
                        used[j] = True
                        merged = True

            cleaned_chain = self._clean_polygon_points(chain, eps=1e-3)
            if len(cleaned_chain) >= 3 and dist(cleaned_chain[0], cleaned_chain[-1]) <= tolerance:
                closed_polygons.append(cleaned_chain)

        return closed_polygons

    def process_geometry(self) -> list:
        """
        Extract closed LWPOLYLINE entities and merged LINE/ARC loops,
        assign 3-digit tags (001, 002, ...), and apply cutting kerf allowances.
        """
        if self.msp is None:
            self.load_dxf()

        kerf = float(self.profile["default_kerf_mm"])
        raw_polygons = []

        for entity in self.msp:
            if entity.dxftype() == "LWPOLYLINE" and entity.closed:
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                except Exception:
                    pts = list(entity.get_points(format="xy"))

                cleaned_pts = self._clean_polygon_points(pts)
                if len(cleaned_pts) >= 3:
                    raw_polygons.append(("Closed Polyline", cleaned_pts))

        merged_polys = self._merge_loose_lines_and_arcs(tolerance=0.5)
        for pts in merged_polys:
            cleaned_pts = self._clean_polygon_points(pts)
            if len(cleaned_pts) >= 3:
                raw_polygons.append(("Merged Line/Arc", cleaned_pts))

        self.extracted_pieces = []
        for idx, (source_type, pts) in enumerate(raw_polygons, start=1):
            net_len, net_wid = self._get_min_wastage_bbox(pts)
            net_area_m2 = self._polygon_area(pts) / 1_000_000.0

            cut_len = round(net_len + kerf, 2)
            cut_wid = round(net_wid + kerf, 2)
            gross_bbox_area_m2 = (cut_len * cut_wid) / 1_000_000.0
            is_rect = self._is_standard_rectangle(pts)

            if net_len > 0 and net_wid > 0:
                self.extracted_pieces.append({
                    "Tag": f"{idx:03d}",
                    "Material": self.profile["name"],
                    "Source Entity": source_type,
                    "Shape Type": "Standard Rect" if is_rect else "Custom / Detail",
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
        """
        Estimate required sheets (2D) or linear bars (1D) and overall wastage percentage.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        total_pieces = len(self.extracted_pieces)
        detail_pieces = sum(1 for p in self.extracted_pieces if p.get("Shape Type") != "Standard Rect")
        total_net_area = sum(p["Net Area (m2)"] for p in self.extracted_pieces)
        total_gross_bbox_area = sum(p["Gross BBox Area (m2)"] for p in self.extracted_pieces)

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
        Generate dedicated Detail Sheets in DXF for custom/non-rectangular pieces
        with frame borders, 3-digit tags (no circles), and overall dimensions.
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
                    f"DETAIL SHEET {sheet_idx + 1:02d} | {self.profile['name']}",
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
                f"TAG {piece['Tag']} | L:{piece['Net Length (mm)']} x W:{piece['Net Width (mm)']} mm",
                dxfattribs={
                    "layer": "GAFY_DETAIL_TAGS",
                    "height": 14.0,
                    "insert": (cell_origin_x + 15.0, cell_origin_y + 12.0)
                }
            )

        out_doc.saveas(output_dxf_path)
        return output_dxf_path
