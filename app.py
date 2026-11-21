import os
import io
import tempfile
import ezdxf
from ezdxf.path import make_path
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Unified Fabrication Engine",
    layout="wide",
    page_icon="⚙️"
)

st.title("⚙️ Unified Fabrication & Cutlist Engine")
st.markdown("Automated DXF shop drawing processing and cutting list generation for marble, wood, aluminum, and metals.")

st.sidebar.header("Fabrication Settings")
material_type = st.sidebar.selectbox(
    "Select Material Profile:",
    ["marble_granite", "mdf_wood", "aluminum_profile", "stainless_steel"]
)

default_kerfs = {
    "marble_granite": 4.0,
    "mdf_wood": 3.2,
    "aluminum_profile": 5.0,
    "stainless_steel": 1.5
}

kerf_val = st.sidebar.number_input(
    "Kerf / Cutting Allowance (mm):",
    min_value=0.0,
    value=float(default_kerfs.get(material_type, 4.0)),
    step=0.5
)

uploaded_file = st.file_uploader("Upload DXF Shop Drawing:", type=["dxf"])

if uploaded_file is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_file:
        tmp_file.write(uploaded_file.getvalue())
        tmp_path = tmp_file.name

    st.success(f"Uploaded: {uploaded_file.name}")

    if st.button("🚀 Run Fabrication Engine"):
        with st.spinner("Processing DXF geometry..."):
            try:
                doc = ezdxf.readfile(tmp_path)
                msp = doc.modelspace()

                processed_pieces = []
                counter = 1

                for entity in msp:
                    if entity.dxftype() == "LWPOLYLINE" and entity.closed:
                        points = list(entity.get_points(format="xy"))
                        if len(points) >= 3:
                            xs = [p[0] for p in points]
                            ys = [p[1] for p in points]
                            width = max(xs) - min(xs)
                            height = max(ys) - min(ys)
                            if width > 0 and height > 0:
                                processed_pieces.append({
                                    "Tag": f"{counter:03d}",
                                    "Material": material_type,
                                    "Source": "Closed Polyline",
                                    "Cut Width (mm)": round(width + kerf_val, 2),
                                    "Cut Height (mm)": round(height + kerf_val, 2),
                                    "Area (m2)": round((width * height) / 1_000_000.0, 4)
                                })
                                counter += 1

                for entity in msp:
                    if entity.dxftype() in ["LINE", "ARC"]:
                        try:
                            path = make_path(entity)
                            if path.is_closed:
                                flattened = [(v.x, v.y) for v in path.flattening(distance=0.5)]
                                if len(flattened) >= 3:
                                    xs = [p[0] for p in flattened]
                                    ys = [p[1] for p in flattened]
                                    w = max(xs) - min(xs)
                                    h = max(ys) - min(ys)
                                    if w > 0 and h > 0:
                                        processed_pieces.append({
                                            "Tag": f"{counter:03d}",
                                            "Material": material_type,
                                            "Source": "Merged Line/Arc",
                                            "Cut Width (mm)": round(w + kerf_val, 2),
                                            "Cut Height (mm)": round(h + kerf_val, 2),
                                            "Area (m2)": round((w * h) / 1_000_000.0, 4)
                                        })
                                        counter += 1
                        except Exception:
                            continue

                if processed_pieces:
                    df = pd.DataFrame(processed_pieces)
                    st.subheader("Cutlist Schedule")
                    st.dataframe(df, use_container_width=True)

                    col1, col2 = st.columns(2)
                    col1.metric("Total Pieces", len(df))
                    col2.metric("Total Net Area (m²)", round(df["Area (m2)"].sum(), 4))

                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine="openpyxl") as writer:
                        df.to_excel(writer, index=False, sheet_name="Cutlist")
                    excel_data = output.getvalue()

                    st.download_button(
                        label="📥 Download Excel Cutlist",
                        data=excel_data,
                        file_name=f"Cutlist_{material_type}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.warning("No closed geometric pieces found in this DXF file.")

            except Exception as e:
                st.error(f"Error processing DXF: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
