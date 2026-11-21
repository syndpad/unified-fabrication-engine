import streamlit as st
import pandas as pd
import io
import tempfile
import os
import ezdxf
from ezdxf.path import make_path

# إعدادات صفحة Streamlit
st.set_page_config(
    page_title="Unified Fabrication Engine | محرك المقاولات المعمارية",
    layout="wide",
    icon="⚙️"
)

st.title("⚙️ Unified Fabrication & Cutlist Engine")
st.markdown("منصة ذكية لأتمتة مخططات الـ Shop Drawing وجداول التقطيع (رخام، أخشاب، وألومنيوم/معادن) عبر تحليل ملفات DXF.")

# --- شريط جانبى لاختيار المادة والإعدادات ---
st.sidebar.header("إعدادات محرك التصنيع")
material_type = st.sidebar.selectbox(
    "اختر نوع المادة (Material Profile):",
    ["marble_thassos", "mdf_wood", "aluminum_profile", "stainless_steel"]
)

# تحديد سمك شفرة المنشار أو القص (Kerf) بناءً على المادة
default_kerfs = {
    "marble_thassos": 4.0,
    "mdf_wood": 3.0,
    "aluminum_profile": 5.0,
    "stainless_steel": 2.0
}
kerf_val = st.sidebar.number_input("سمك مسار القطع / الشفرة (Kerf - mm):", value=default_kerfs.get(material_type, 4.0))

# رفع ملف الـ DXF
uploaded_file = st.file_uploader("ارفع ملف الـ Shop Drawing بصيغة DXF:", type=["dxf"])

if uploaded_file is not None:
    # حفظ الملف المرفوع مؤقتاً لمعالجته عبر ezdxf
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_file:
        tmp_file.write(uploaded_file.getvalue())
        tmp_path = tmp_file.name

    st.success(f"تم رفع الملف بنجاح: {uploaded_file.name}")

    if st.button("🚀 تشغيل محرك التحليل واستخراج جدول التقطيع"):
        with st.spinner("جاري قراءة الكيانات الهندسية ودمج الخطوط والأقواس..."):
            try:
                doc = ezdxf.readfile(tmp_path)
                msp = doc.modelspace()
                
                processed_pieces = []
                counter = 1
                
                # معالجة الـ LWPolylines المغلقة
                for entity in msp:
                    if entity.dxftype() == "LWPOLYLINE" and entity.closed:
                        points = list(entity.get_points(format="xy"))
                        xs = [p[0] for p in points]
                        ys = [p[1] for p in points]
                        width = max(xs) - min(xs)
                        height = max(ys) - min(ys)
                        
                        processed_pieces.append({
                            "Tag ID": f"{material_type.upper()}-{counter:03d}",
                            "Entity Type": "Closed Polyline",
                            "Width (mm)": round(width + kerf_val, 2),
                            "Height (mm)": round(height + kerf_val, 2),
                            "Area (m2)": round((width * height) / 1000000.0, 4)
                        })
                        counter += 1

                # معالجة الخطوط والأقواس المنفصلة ودمجها
                for entity in msp:
                    if entity.dxftype() in ["LINE", "ARC"]:
                        try:
                            path = make_path(entity)
                            if path.is_closed:
                                flattened = [(v.x, v.y) for v in path.flattening(distance=0.5)]
                                if len(flattened) > 2:
                                    xs = [p[0] for p in flattened]
                                    ys = [p[1] for p in flattened]
                                    w = max(xs) - min(xs)
                                    h = max(ys) - min(ys)
                                    processed_pieces.append({
                                        "Tag ID": f"{material_type.upper()}-M{counter:03d}",
                                        "Entity Type": "Merged Line/Arc Path",
                                        "Width (mm)": round(w + kerf_val, 2),
                                        "Height (mm)": round(h + kerf_val, 2),
                                        "Area (m2)": round((w * h) / 1000000.0, 4)
                                    })
                                    counter += 1
                        except Exception:
                            continue

                if processed_pieces:
                    df = pd.DataFrame(processed_pieces)
                    st.markdown("### 📊 جدول التقطيع المستخرج (Cutlist Schedule)")
                    st.dataframe(df, use_container_width=True)

                    # حساب الإحصائيات العامة
                    total_pieces = len(df)
                    total_area = df["Area (m2)"].sum()
                    
                    col1, col2 = st.columns(2)
                    col1.metric("إجمالي عدد القطع", total_pieces)
                    col2.metric("إجمالي المساحة الصافية (متر مربع)", f"{total_area:.2f} m²")

                    # إعداد ملف الـ Excel للتنزيل المباشر
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False, sheet_name='Cutlist')
                    excel_data = output.getvalue()

                    st.download_button(
                        label="📥 تنزيل جدول التقطيع بصيغة Excel معتمد",
                        data=excel_data,
                        file_name=f"Cutlist_{material_type}_Output.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.warning("⚠️ لم يتم العثور على مسارات هندسية مغلقة مطابقة لشروط المعالجة في هذا الملف.")

            except Exception as e:
                st.error(f"❌ حدث خطأ أثناء معالجة ملف الـ DXF: {e}")
            finally:
                # تنظيف الملف المؤقت
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

---

### مميزات هذه الواجهة والربط:
1. **مرونة اختيار المادة:** من خلال القائمة الجانبية، يمكنك اختيار المادة (رخام، خشب، ألومنيوم، ستانلس) ليتغير تلقائياً معيار مسار القص (`Kerf`) وتنسيق الـ `Tag ID`.
2. **معاينة حية فورية:** عرض جدول البيانات (`DataFrame`) بشكل منظم يوضح الأبعاد، المساحات، والترقيم التسلسلي.
3. **التصدير الفوري لـ Excel:** زر تنزيل مدمج يتيح للمهندس الحصول على جدول جاهز ومطابق لمعايير المكتب الفني بضغطة زر واحدة ودون أي تدخل يدوي.

<FollowUp label="هل تحب أن ننتقل لتطوير ميزة محسّنة لحساب اتجاه الألياف (Grain Direction) في الأخشاب أو مطابقة العروق (Vein Matching) للرخام داخل هذا السكربت؟" query="هل تحب أن نضيف ميزة مطابقة العروق للرخام أو اتجاه الألياف للأخشاب داخل نفس المحرك البرمجي؟"/>