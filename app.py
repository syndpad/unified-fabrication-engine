import os
import tempfile
import pandas as pd
import streamlit as st
from unified_engine_core import UnifiedFabricationEngine

st.set_page_config(
    page_title="Gafy AI Engine | Unified Fabrication Platform",
    layout="wide",
    page_icon="⚙️"
)

LOGO_CANDIDATES = [
    "Gafy_Logo.png",
    "gafy_logo.png",
    "logo.png",
    "brand_logo.png",
    "logo.jpg",
    "logo.jpeg"
]


def find_logo_file() -> str | None:
    for candidate in LOGO_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


logo_path = find_logo_file()

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.4rem;
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
    """,
    unsafe_allow_html=True
)

MATERIAL_OPTIONS = {
    "marble_granite": "Marble & Granite",
    "mdf_wood": "MDF & Wood Joinery",
    "aluminum_profile": "Aluminum Profiles",
    "stainless_steel": "Stainless Steel / Metals"
}

DEFAULT_KERFS = {
    "marble_granite": 4.0,
    "mdf_wood": 3.2,
    "aluminum_profile": 5.0,
    "stainless_steel": 1.5
}

if "fabrication_results" not in st.session_state:
    st.session_state.fabrication_results = None

if logo_path:
    st.sidebar.image(logo_path, use_container_width=True)

st.sidebar.header("Brand & Fabrication Controls")
brand_name = st.sidebar.text_input("Brand Name", value="Gafy AI Engine")

material_key = st.sidebar.selectbox(
    "Material Profile",
    options=list(MATERIAL_OPTIONS.keys()),
    format_func=lambda k: MATERIAL_OPTIONS[k]
)

kerf_val = st.sidebar.number_input(
    "Kerf / Cutting Allowance (mm)",
    min_value=0.0,
    max_value=50.0,
    value=float(DEFAULT_KERFS[material_key]),
    step=0.5
)

banner_left, banner_right = st.columns([5, 1], vertical_alignment="center")
with banner_left:
    st.markdown(
        f"""
        <div class="header-card">
            <h1>⚙️ {brand_name} — Unified Fabrication &amp; Cutlist Engine</h1>
            <p>Automated DXF geometry extraction, line/arc merging, sheet estimation, shop drawings, and detail sheets export.</p>
        </div>
        """,
        unsafe_allow_html=True
    )
with banner_right:
    if logo_path:
        st.image(logo_path, use_container_width=True)

left_col, right_col = st.columns([2, 1], gap="large")

with left_col:
    uploaded_file = st.file_uploader("Upload DXF Shop Drawing (.dxf)", type=["dxf"])

with right_col:
    st.markdown("**Active Profile Summary**")
    st.write(f"**Brand:** {brand_name}")
    st.write(f"**Material:** {MATERIAL_OPTIONS[material_key]}")
    st.write(f"**Kerf:** `{kerf_val:.1f} mm`")

if uploaded_file is not None:
    current_sig = f"{uploaded_file.name}_{uploaded_file.size}_{material_key}_{kerf_val}"
    if (
        st.session_state.fabrication_results is not None
        and st.session_state.fabrication_results.get("signature") != current_sig
    ):
        st.session_state.fabrication_results = None

    st.success(f"Loaded drawing: `{uploaded_file.name}`")

    if st.button("🚀 Run Fabrication Engine", type="primary"):
        with st.spinner("Processing geometry, calculating sheets & wastage, and generating CAD/Excel deliverables..."):
            tmp_dxf_path = None
            tmp_excel_path = None
            tmp_shop_dxf_path = None
            tmp_detail_dxf_path = None

            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp_file:
                    tmp_file.write(uploaded_file.getvalue())
                    tmp_dxf_path = tmp_file.name

                tmp_excel_path = tmp_dxf_path.replace(".dxf", "_Cutlist.xlsx")
                tmp_shop_dxf_path = tmp_dxf_path.replace(".dxf", "_Tagged_ShopDrawing.dxf")
                tmp_detail_dxf_path = tmp_dxf_path.replace(".dxf", "_Detail_Sheets.dxf")

                engine = UnifiedFabricationEngine(
                    dxf_path=tmp_dxf_path,
                    material_key=material_key,
                    custom_kerf=kerf_val
                )
                pieces = engine.process_geometry()
                summary = engine.calculate_sheets_and_wastage()

                if pieces:
                    clean_rows = [
                        {k: v for k, v in p.items() if k != "Polygon Points"}
                        for p in pieces
                    ]
                    df_cutlist = pd.DataFrame(clean_rows)
                    df_summary = pd.DataFrame(
                        list(summary.items()),
                        columns=["Metric", "Value"]
                    )

                    engine.export_to_excel(tmp_excel_path)
                    engine.export_processed_dxf(tmp_shop_dxf_path)
                    engine.export_detail_sheets_dxf(tmp_detail_dxf_path)

                    with open(tmp_excel_path, "rb") as f:
                        excel_bytes = f.read()
                    with open(tmp_shop_dxf_path, "rb") as f:
                        shop_dxf_bytes = f.read()
                    with open(tmp_detail_dxf_path, "rb") as f:
                        detail_dxf_bytes = f.read()

                    units_count = summary.get(
                        "Estimated Sheets Required",
                        summary.get("Estimated Bars Required", 0)
                    )
                    wastage_pct = summary.get("Estimated Wastage (%)", 0.0)

                    st.session_state.fabrication_results = {
                        "signature": current_sig,
                        "material_key": material_key,
                        "total_pieces": len(df_cutlist),
                        "total_net_area": float(df_cutlist["Net Area (m2)"].sum()),
                        "units_count": units_count,
                        "wastage_pct": float(wastage_pct),
                        "df_cutlist": df_cutlist,
                        "df_summary": df_summary,
                        "excel_bytes": excel_bytes,
                        "shop_dxf_bytes": shop_dxf_bytes,
                        "detail_dxf_bytes": detail_dxf_bytes
                    }
                else:
                    st.session_state.fabrication_results = None
                    st.warning("No closed geometric pieces were detected in the uploaded DXF file.")

            except Exception as e:
                st.session_state.fabrication_results = None
                st.error(f"Error processing DXF file: {e}")
            finally:
                for path in [tmp_dxf_path, tmp_excel_path, tmp_shop_dxf_path, tmp_detail_dxf_path]:
                    if path and os.path.exists(path):
                        os.remove(path)

results = st.session_state.fabrication_results
if results is not None:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Pieces", results["total_pieces"])
    m2.metric("Total Net Area (m²)", f"{results['total_net_area']:.4f}")
    m3.metric("Estimated Units", results["units_count"])
    m4.metric("Estimated Wastage", f"{results['wastage_pct']:.2f}%")

    tab1, tab2, tab3 = st.tabs([
        "📋 Cutlist Schedule",
        "📦 Sheets & Wastage",
        "📥 Export Deliverables"
    ])

    with tab1:
        st.dataframe(results["df_cutlist"], use_container_width=True, hide_index=True)

    with tab2:
        st.dataframe(results["df_summary"], use_container_width=True, hide_index=True)

    with tab3:
        st.write("Download all generated deliverables without re-running the engine:")
        d1, d2, d3 = st.columns(3)
        res_mat = results["material_key"]

        with d1:
            st.download_button(
                label="📊 Download Excel Cutlist (.xlsx)",
                data=results["excel_bytes"],
                file_name=f"Gafy_Cutlist_{res_mat}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="dl_excel_btn"
            )

        with d2:
            st.download_button(
                label="📐 Download Shop Drawing (.dxf)",
                data=results["shop_dxf_bytes"],
                file_name=f"Gafy_Tagged_ShopDrawing_{res_mat}.dxf",
                mime="application/dxf",
                use_container_width=True,
                key="dl_shop_dxf_btn"
            )

        with d3:
            st.download_button(
                label="🗂️ Download Detail Sheets (.dxf)",
                data=results["detail_dxf_bytes"],
                file_name=f"Gafy_Detail_Sheets_{res_mat}.dxf",
                mime="application/dxf",
                use_container_width=True,
                key="dl_detail_dxf_btn"
            )
