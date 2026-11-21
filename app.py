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

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    .header-card {
        background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
        color: #ffffff;
        padding: 1.25rem 1.5rem;
        border-radius: 12px;
        margin-bottom: 1.2rem;
        border-left: 5px solid #38bdf8;
    }
    .header-card h1 {
        margin: 0 0 0.35rem 0;
        font-size: 1.65rem;
        color: #ffffff;
    }
    .header-card p {
        margin: 0;
        color: #cbd5e1;
        font-size: 0.95rem;
    }
    div[data-testid="stMetric"] {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        padding: 0.85rem 1rem;
        border-radius: 10px;
    }
    </style>
    <div class="header-card">
        <h1>⚙️ Unified Fabrication &amp; Cutlist Engine</h1>
        <p>Automated DXF geometry extraction, line/arc merging, and production cutlist export across multi-material contracting.</p>
    </div>
    """,
    unsafe_allow_html=True
)

MATERIAL_PROFILES = {
    "marble_granite": {
        "label": "Marble & Granite",
        "kerf": 4.0,
        "allowance_note": "Saw kerf + polishing/vein alignment allowance"
    },
    "mdf_wood": {
        "label": "MDF & Wood Joinery",
        "kerf": 3.2,
        "allowance_note": "Panel saw kerf + grain/edge banding clearance"
    },
    "aluminum_profile": {
        "label": "Aluminum Profiles",
        "kerf": 5.0,
        "allowance_note": "Blade kerf + fabrication/milling clearance"
    },
    "stainless_steel": {
        "label": "Stainless Steel / Metals",
        "kerf": 1.5,
        "allowance_note": "Laser/plasma kerf + forming tolerance"
    }
}

st.sidebar.header("Fabrication Controls")
material_key = st.sidebar.selectbox(
    "Material Profile",
    options=list(MATERIAL_PROFILES.keys()),
    format_func=lambda k: MATERIAL_PROFILES[k]["label"]
)

kerf_val = st.sidebar.number_input(
    "Kerf / Cutting Allowance (mm)",
    min_value=0.0,
    max_value=50.0,
    value=float(MATERIAL_PROFILES[material_key]["kerf"]),
    step=0.5
)

st.sidebar.info(MATERIAL_PROFILES[material_key]["allowance_note"])

left_col, right_col = st.columns([2, 1], gap="large")

with left_col:
    uploaded_file = st.file_uploader("Upload DXF Shop Drawing (.dxf)", type=["dxf"])

with right_col:
    st.markdown("**Active Profile Summary**")
    st.write(f"**Material:** {MATERIAL_PROFILES[material_key]['label']}")
    st.write(f"**Kerf:** `{kerf_val:.1f} mm`")

if uploaded_file is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_file:
        tmp_file.write(uploaded_file.getvalue())
        tmp_path = tmp_file.name

    st.success(f"Loaded drawing: `{uploaded_file.name}`")

    if st.button("🚀 Run Fabrication Engine", type="primary"):
        with st.spinner("Extracting closed polylines and merging connected lines/arcs..."):
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
                                    "Material": MATERIAL_PROFILES[material_key]["label"],
                                    "Source": "Closed Polyline",
                                    "Cut Width (mm)": round(width + kerf_val, 2),
                                    "Cut Height (mm)": round(height + kerf_val, 2),
                                    "Net Area (m2)": round((width * height) / 1_000_000.0, 4)
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
                                            "Material": MATERIAL_PROFILES[material_key]["label"],
                                            "Source": "Merged Line/Arc",
                                            "Cut Width (mm)": round(w + kerf_val, 2),
                                            "Cut Height (mm)": round(h + kerf_val, 2),
                                            "Net Area (m2)": round((w * h) / 1_000_000.0, 4)
                                        })
                                        counter += 1
                        except Exception:
                            continue

                if processed_pieces:
                    df = pd.DataFrame(processed_pieces)

                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Pieces", len(df))
                    m2.metric("Total Net Area (m²)", f"{df['Net Area (m2)'].sum():.4f}")
                    m3.metric("Applied Kerf", f"{kerf_val:.1f} mm")

                    tab1, tab2, tab3 = st.tabs([
                        "📋 Cutlist Schedule",
                        "📐 Source Breakdown",
                        "📄 Export Excel"
                    ])

                    with tab1:
                        st.dataframe(df, use_container_width=True, hide_index=True)

                    with tab2:
                        source_counts = df["Source"].value_counts().reset_index()
                        source_counts.columns = ["Entity Source", "Piece Count"]
                        st.dataframe(source_counts, use_container_width=True, hide_index=True)

                    with tab3:
                        output = io.BytesIO()
                        with pd.ExcelWriter(output, engine="openpyxl") as writer:
                            df.to_excel(writer, index=False, sheet_name="Cutlist")
                        excel_data = output.getvalue()

                        st.write("Download the formatted production schedule for shop floor or technical review:")
                        st.download_button(
                            label="📥 Download Excel Cutlist (.xlsx)",
                            data=excel_data,
                            file_name=f"Cutlist_{material_key}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                else:
                    st.warning("No closed geometric pieces were detected in the uploaded DXF file.")

            except Exception as e:
                st.error(f"Error processing DXF file: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
