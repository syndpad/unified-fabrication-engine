import os
import json
import math
import ezdxf
from ezdxf.path import make_path, from_hatch_boundary_path
from ezdxf.math import BoundingBox2d
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


class UnifiedFabricationEngine:
    """
    النواة الموحدة لأتمتة وتصنيع المواد المعمارية (Gafy Multi-Material Engine)
    تدعم: الرخام والجرانيت، الأخشاب، الألومنيوم، والمعادن المعمارية.
    """

    DEFAULT_PROFILES = {
        "marble_granite": {
            "name": "Marble & Granite (رخام وجرانيت)",
            "mode": "plate_2d",
            "default_kerf_mm": 4.0,
            "sheet_length_mm": 2800.0,
            "sheet_width_mm": 1600.0,
            "wastage_factor": 1.12,
            "notes": "Vein matching & polishing allowance applied"
        },
        "mdf_wood": {
            "name": "MDF & Wood Joinery (أخشاب ونجارة)",
            "mode": "plate_2d",
            "default_kerf_mm": 3.2,
            "sheet_length_mm": 2440.0,
            "sheet_width_mm": 1220.0,
            "wastage_factor": 1.08,
            "notes": "Grain direction & edge banding rules"
        },
        "stainless_steel": {
            "name": "Architectural Metals / Stainless (معادن وستانلس)",
            "mode": "plate_2d",
            "default_kerf_mm": 1.5,
            "sheet_length_mm": 3000.0,
            "sheet_width_mm": 1500.0,
            "wastage_factor": 1.05,
            "notes": "Laser cutting kerf & bending clearance"
        },
        "aluminum_profile": {
            "name": "Aluminum Profiles (قطاعات ألومنيوم)",
            "mode": "linear_1d",
            "default_kerf_mm": 5.0,
            "standard_bar_mm": 6000.0,
            "wastage_factor": 1.06,
            "notes": "1D Linear bar optimization"
        }
    }

    def __init__(self, dxf_path: str, material_key: str = "marble_granite", custom_kerf: float = None):
        self.dxf_path = dxf_path
        self.material_key = material_key
        self.profile = self.DEFAULT_PROFILES.get(material_key, self.DEFAULT_PROFILES["marble_granite"]).copy()
        
        if custom_kerf is not None:
            self.profile["default_kerf_mm"] = float(custom_kerf)

        self.doc = None
        self.msp = None
        self.extracted_pieces = []
        self.summary_metrics = {}

    def load_dxf(self):
        """قراءة ملف الـ DXF والتحقق من سلامته"""
        if not os.path.exists(self.dxf_path):
            raise FileNotFoundError(f"الملف غير موجود في المسار: {self.dxf_path}")
        
        self.doc = ezdxf.readfile(self.dxf_path)
        self.msp = self.doc.modelspace()

    def _get_min_wastage_bbox(self, points: list) -> tuple:
        """
        حساب أبعاد صندوق الاحتواء بأقل هدر (Minimum Bounding Box)
        باستخدام تدوير الزوايا للقطع غير المنتظمة لتقليل الهدر الفعلي في اللوح.
        """
        if len(points) < 3:
            return 0.0, 0.0

        min_area = float("inf")
        best_w, best_h = 0.0, 0.0

        # فحص الزوايا كل 5 درجات لتحديد أفضل توجيه للقطعة على اللوح
        for angle_deg in range(0, 90, 5):
            rad = math.radians(angle_deg)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            
            rot_x = [p[0] * cos_a - p[1] * sin_a for p in points]
            rot_y = [p[0] * sin_a + p[1] * cos_a for p in points]
            
            w = max(rot_x) - min(rot_x)
            h = max(rot_y) - min(rot_y)
            area = w * h
            
            if area < min_area and area > 0:
                min_area = area
                best_w, best_h = max(w, h), min(w, h)

        return round(best_w, 2), round(best_h, 2)

    def _polygon_area(self, points: list) -> float:
        """حساب المساحة الهندسية الصافية للمضلع بالمليمتر المربع"""
        n = len(points)
        if n < 3:
            return 0.0
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += points[i][0] * points[j][1]
            area -= points[j][0] * points[i][1]
        return abs(area) / 2.0

    def _merge_loose_lines_and_arcs(self, tolerance: float = 0.5) -> list:
        """
        تجميع الخطوط (LINE) والأقواس (ARC) المنفصلة المتصلة عند نهاياتها
        لتحويلها إلى مسارات مغلقة LWPolylines جاهزة للتصنيع.
        """
        loose_paths = []
        for entity in self.msp:
            if entity.dxftype() in ("LINE", "ARC"):
                try:
                    p = make_path(entity)
                    pts = [(round(v.x, 3), round(v.y, 3)) for v in p.flattening(distance=0.5)]
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

            # التحقق من إغلاق المسار
            if len(chain) >= 3 and dist(chain[0], chain[-1]) <= tolerance:
                closed_polygons.append(chain)

        return closed_polygons

    def process_geometry(self) -> list:
        """
        استخراج كافة القطع الهندسية (Polylines مغلقة + الخطوط والأقواس المدمجة)
        وتطبيق الترقيم الثلاثي (001, 002) وإضافة سماحية شفرة القص (Kerf).
        """
        if self.msp is None:
            self.load_dxf()

        kerf = self.profile["default_kerf_mm"]
        raw_polygons = []

        # 1. استخراج LWPolylines المغلقة أصلاً
        for entity in self.msp:
            if entity.dxftype() == "LWPOLYLINE" and entity.closed:
                try:
                    p = make_path(entity)
                    pts = [(v.x, v.y) for v in p.flattening(distance=0.5)]
                    if len(pts) >= 3:
                        raw_polygons.append(("LWPolyline", pts))
                except Exception:
                    pts = list(entity.get_points(format="xy"))
                    if len(pts) >= 3:
                        raw_polygons.append(("LWPolyline", pts))

        # 2. استخراج ودمج الخطوط والأقواس المنفصلة
        merged_polys = self._merge_loose_lines_and_arcs(tolerance=0.5)
        for pts in merged_polys:
            raw_polygons.append(("Merged Line/Arc", pts))

        # 3. بناء سجل القطع الموحد
        self.extracted_pieces = []
        for idx, (source_type, pts) in enumerate(raw_polygons, start=1):
            net_len, net_wid = self._get_min_wastage_bbox(pts)
            net_area_m2 = self._polygon_area(pts) / 1_000_000.0
            
            # الأبعاد شاملة سماحية المنشار/الليزر (Cutting Allowance)
            cut_len = round(net_len + kerf, 2)
            cut_wid = round(net_wid + kerf, 2)
            gross_bbox_area_m2 = (cut_len * cut_wid) / 1_000_000.0

            if net_len > 0 and net_wid > 0:
                self.extracted_pieces.append({
                    "Tag": f"{idx:03d}",  # ترقيم ثلاثي موحد (001, 002)
                    "Material": self.profile["name"],
                    "Source Entity": source_type,
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
        حساب عدد الألواح القياسية (أو الأعواد الطولية) المطلوبة ونسبة الهدر الإجمالية.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        total_pieces = len(self.extracted_pieces)
        total_net_area = sum(p["Net Area (m2)"] for p in self.extracted_pieces)
        total_gross_bbox_area = sum(p["Gross BBox Area (m2)"] for p in self.extracted_pieces)

        if self.profile["mode"] == "plate_2d":
            sheet_l = self.profile["sheet_length_mm"]
            sheet_w = self.profile["sheet_width_mm"]
            single_sheet_area_m2 = (sheet_l * sheet_w) / 1_000_000.0
            
            # تقدير الألواح المطلوبة مع معامل الترصيص (Nesting Efficiency ~85%)
            effective_required_area = total_gross_bbox_area * self.profile["wastage_factor"]
            estimated_sheets = math.ceil(effective_required_area / single_sheet_area_m2) if total_pieces > 0 else 0
            
            total_purchased_area = estimated_sheets * single_sheet_area_m2
            wastage_area = max(0.0, total_purchased_area - total_net_area)
            wastage_pct = (wastage_area / total_purchased_area * 100.0) if total_purchased_area > 0 else 0.0

            self.summary_metrics = {
                "Material Profile": self.profile["name"],
                "Total Pieces": total_pieces,
                "Standard Sheet Size (mm)": f"{int(sheet_l)} x {int(sheet_w)}",
                "Single Sheet Area (m2)": round(single_sheet_area_m2, 3),
                "Total Net Area (m2)": round(total_net_area, 3),
                "Total BBox Area + Kerf (m2)": round(total_gross_bbox_area, 3),
                "Estimated Sheets Required": estimated_sheets,
                "Total Raw Material Area (m2)": round(total_purchased_area, 3),
                "Estimated Wastage (%)": round(wastage_pct, 2)
            }
        else:
            # حساب التقطيع الطولي 1D (للقطاعات المعدنية والألومنيوم)
            bar_len = self.profile["standard_bar_mm"]
            total_linear_mm = sum(p["Cut Length + Kerf (mm)"] for p in self.extracted_pieces)
            estimated_bars = math.ceil((total_linear_mm * self.profile["wastage_factor"]) / bar_len) if total_pieces > 0 else 0
            total_purchased_mm = estimated_bars * bar_len
            wastage_pct = ((total_purchased_mm - total_linear_mm) / total_purchased_mm * 100.0) if total_purchased_mm > 0 else 0.0

            self.summary_metrics = {
                "Material Profile": self.profile["name"],
                "Total Pieces": total_pieces,
                "Standard Bar Length (mm)": int(bar_len),
                "Total Net Length (m)": round(total_linear_mm / 1000.0, 2),
                "Estimated Bars Required": estimated_bars,
                "Estimated Wastage (%)": round(wastage_pct, 2)
            }

        return self.summary_metrics

    def export_to_excel(self, output_excel_path: str = "Unified_Cutlist_Report.xlsx") -> str:
        """
        تصدير جدول التقطيع وتقرير حساب الألواح والهدر إلى ملف Excel منسق باحترافية.
        """
        if not self.summary_metrics:
            self.calculate_sheets_and_wastage()

        # تجهيز جدول القطع (بدون إحداثيات النقاط الخام)
        clean_rows = [{k: v for k, v in p.items() if k != "Polygon Points"} for p in self.extracted_pieces]
        df_cutlist = pd.DataFrame(clean_rows)
        df_summary = pd.DataFrame(list(self.summary_metrics.items()), columns=["Parameter", "Value"])

        with pd.ExcelWriter(output_excel_path, engine="openpyxl") as writer:
            df_cutlist.to_excel(writer, index=False, sheet_name="Cutting_List")
            df_summary.to_excel(writer, index=False, sheet_name="Sheets_&_Wastage_Summary")

            # تنسيق الجداول باستخدام openpyxl
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

                # ضبط عرض الأعمدة تلقائياً
                for col in ws.columns:
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws.column_dimensions[col[0].column_letter].width = max(max_len + 4, 14)

        return output_excel_path

    def export_processed_dxf(self, output_dxf_path: str = "Processed_Tagged_Drawing.dxf") -> str:
        """
        حفظ نسخة DXF محدثة تحتوي على المسارات المغلقة والترقيم الثلاثي (001, 002)
        في طبقات مخصصة جاهزة للإنتاج.
        """
        if not self.extracted_pieces:
            self.process_geometry()

        out_doc = ezdxf.new(dxfversion="R2010")
        out_msp = out_doc.modelspace()

        # إنشاء طبقات قياسية
        if "GAFY_CUT_POLYLINES" not in out_doc.layers:
            out_doc.layers.add("GAFY_CUT_POLYLINES", color=3)  # أخضر
        if "GAFY_PART_TAGS" not in out_doc.layers:
            out_doc.layers.add("GAFY_PART_TAGS", color=2)      # أصفر

        for piece in self.extracted_pieces:
            pts = piece["Polygon Points"]
            # رسم البوليلاين المغلق
            out_msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "GAFY_CUT_POLYLINES"})
            
            # حساب مركز القطعة لوضع الترقيم الثلاثي بدون دائرة
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            text_height = max(min(piece["Net Width (mm)"] * 0.12, 60.0), 15.0)
            
            out_msp.add_text(
                piece["Tag"],
                dxfattribs={
                    "layer": "GAFY_PART_TAGS",
                    "height": text_height,
                    "insert": (cx, cy)
                }
            )

        out_doc.saveas(output_dxf_path)
        return output_dxf_path


# --- مثال التشغيل المباشر (CLI / Local Test) ---
if __name__ == "__main__":
    sample_file = "sample_shop_drawing.dxf"
    
    if os.path.exists(sample_file):
        # يمكنك تغيير المادة إلى: marble_granite, mdf_wood, stainless_steel, aluminum_profile
        engine = UnifiedFabricationEngine(dxf_path=sample_file, material_key="marble_granite")
        pieces = engine.process_geometry()
        summary = engine.calculate_sheets_and_wastage()
        
        excel_file = engine.export_to_excel("Output_Cutlist_Report.xlsx")
        dxf_file = engine.export_processed_dxf("Output_Tagged_ShopDrawing.dxf")
        
        print("\n=== ملخص حساب الألواح والهدر ===")
        for k, v in summary.items():
            print(f"{k}: {v}")
        print(f"\n[✔] تم إنشاء ملف الإكسل: {excel_file}")
        print(f"[✔] تم إنشاء ملف الأوتوكاد المرقم: {dxf_file}")
    else:
        print(f"جاهز للتشغيل: ضع ملف DXF باسم '{sample_file}' أو استورد الكلاس في تطبيق Streamlit.")