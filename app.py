import os
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

from overlap import TIERS, cost_estimate, find_overlaps, load_projects
from storage import (
    configured_database_url,
    get_engine,
    initialize_database,
    load_opportunities,
    load_project_records,
    load_recent_events,
    replace_project_records,
    save_opportunity,
    update_opportunity_status,
)

UTILITY_COLORS = {"DESC": "#1D4E89", "GPC": "#2A7F62"}
UTILITY_NAMES = {
    "DESC": "Dominion Energy South Carolina (DESC)",
    "GPC": "Georgia Power (GPC)",
}
GEMINI_MODEL = "gemini-2.5-flash"
SEED_FILE = Path(__file__).resolve().parent / "data" / "sample_projects.csv"
PIPELINE_STATUSES = ["Saved", "Contacted", "Coordinating", "Completed"]

st.set_page_config(page_title="GridLock | Grid coordination", layout="wide")
st.markdown(
    """
    <style>
      .block-container {max-width: 1440px; padding-top: 2rem; padding-bottom: 3rem;}
      [data-testid="stMetric"] {
        background: #fff; border: 1px solid #e6edf3; padding: 1rem 1.2rem;
        border-radius: 12px;
      }
      h1, h2, h3 {letter-spacing: -0.025em;}
      .hero-note {color: #526274; font-size: 1.08rem; max-width: 820px;}
    </style>
    """,
    unsafe_allow_html=True,
)

engine = get_engine(configured_database_url())
timescale_enabled = initialize_database(engine, SEED_FILE)
projects = load_projects(load_project_records(engine))
opportunities = load_opportunities(engine)
saved_keys = set(opportunities["opportunity_key"]) if not opportunities.empty else set()
using_placeholder_data = projects["name"].str.contains(r"\(placeholder\)", case=False).all()

st.title("GridLock")
st.markdown(
    '<p class="hero-note">Turn nearby utility construction plans into shared '
    'corridors, coordinated crews, and measurable savings.</p>',
    unsafe_allow_html=True,
)
if using_placeholder_data:
    st.warning(
        "Demo data only: project names, locations, and dates are placeholders, "
        "not verified public utility plans. Import public DESC and Georgia Power "
        "filing data before presenting these matches as real."
    )

if "import_notice" in st.session_state:
    st.success(st.session_state.pop("import_notice"))

with st.sidebar:
    st.header("Project data")
    with st.form("project_import"):
        upload = st.file_uploader("Import a project CSV", type="csv")
        import_submitted = st.form_submit_button("Replace project data")
    if import_submitted:
        if upload is None:
            st.warning("Choose a CSV file first.")
        else:
            upload.seek(0)
            try:
                imported_projects = load_projects(upload)
            except ValueError as error:
                st.error(f"Project import failed: {error}")
            else:
                replace_project_records(engine, imported_projects)
                st.session_state["import_notice"] = (
                    f"Imported {len(imported_projects)} projects into the database."
                )
                st.rerun()
    st.caption("Required columns: id, utility, name, kind, voltage_kv, "
               "start_year, end_year, geometry_wkt.")

    st.header("Compare utilities")
    utilities = sorted(projects.utility.unique())
    if len(utilities) >= 2:
        utility_a = st.selectbox(
            "Utility A",
            utilities,
            index=0,
            format_func=lambda utility: UTILITY_NAMES.get(utility, utility),
        )
        utility_b_options = [utility for utility in utilities if utility != utility_a]
        utility_b = st.selectbox(
            "Utility B",
            utility_b_options,
            index=0,
            format_func=lambda utility: UTILITY_NAMES.get(utility, utility),
        )
    else:
        utility_a = utilities[0] if utilities else ""
        utility_b = ""
        st.warning("Import projects from at least two utilities to compare plans.")

    st.header("Savings assumptions")
    row_width_m = st.slider("Right-of-way width (m)", 20, 90, 38)
    land_cost = st.number_input("Land cost per acre ($)", 1000, 200000, 15000, step=1000)
    mobilization = st.number_input(
        "Savings from one shared crew mobilization ($)",
        0,
        2000000,
        250000,
        step=25000,
    )

is_postgres = engine.dialect.name == "postgresql"
is_tiger_data = bool(os.getenv("TIGERDATA_URL"))
if is_postgres and timescale_enabled:
    storage_name = "Tiger Data / TimescaleDB" if is_tiger_data else "PostgreSQL / TimescaleDB"
elif is_postgres:
    storage_name = "Tiger Data / PostgreSQL" if is_tiger_data else "PostgreSQL"
else:
    storage_name = "Local SQLite"
if timescale_enabled:
    st.caption(f"Storage: **{storage_name}** · coordination activity is stored as a TimescaleDB hypertable.")
elif engine.dialect.name == "postgresql":
    st.caption(f"Storage: **{storage_name}** · using PostgreSQL timestamped event history.")
else:
    st.caption(f"Storage: **{storage_name}** · set `TIGERDATA_URL` to connect Tiger Data.")

active_opportunities = (
    opportunities[opportunities["status"] != "Completed"]
    if not opportunities.empty
    else opportunities
)
pipeline_value = (
    int(active_opportunities["estimated_savings"].sum())
    if not active_opportunities.empty
    else 0
)
overlaps = (
    find_overlaps(projects, utility_a, utility_b)
    if utility_a and utility_b
    else projects.iloc[0:0]
)
metrics = st.columns(4)
metrics[0].metric("Planned projects", len(projects))
metrics[1].metric("Nearby opportunities", len(overlaps))
metrics[2].metric("Active pipeline", len(active_opportunities))
metrics[3].metric("Potential savings", f"${pipeline_value:,.0f}")

if overlaps.empty:
    st.info("No projects meet the 40 km coordination threshold for this utility pair.")
else:
    labels = [
        f"#{row['rank']}  {row.name_a} ↔ {row.name_b}  ({row.distance_km} km)"
        for _, row in overlaps.iterrows()
    ]
    choice = st.selectbox(
        "Coordination opportunity",
        range(len(labels)),
        format_func=lambda index: labels[index],
    )
    selected = overlaps.iloc[choice]
    left, right = st.columns([3, 2])

    with left:
        project_points = projects[projects.utility.isin([utility_a, utility_b])]
        map_view = folium.Map(
            location=[
                project_points.geom.apply(lambda geometry: geometry.centroid.y).mean(),
                project_points.geom.apply(lambda geometry: geometry.centroid.x).mean(),
            ],
            zoom_start=7,
            tiles="OpenStreetMap",
        )
        for _, project in project_points.iterrows():
            color = UTILITY_COLORS.get(project.utility, "#555555")
            tooltip = (
                f"{project.id}: {project['name']} "
                f"({project.start_year}-{project.end_year})"
            )
            if project.geom.geom_type == "Point":
                folium.CircleMarker(
                    [project.geom.y, project.geom.x],
                    radius=7,
                    color=color,
                    fill=True,
                    fill_opacity=0.9,
                    tooltip=tooltip,
                ).add_to(map_view)
            else:
                folium.PolyLine(
                    [(latitude, longitude) for longitude, latitude in project.geom.coords],
                    color=color,
                    weight=5,
                    tooltip=tooltip,
                ).add_to(map_view)
        for index, overlap in overlaps.iterrows():
            is_selected = index == choice
            folium.PolyLine(
                overlap.link,
                color=overlap.color,
                weight=6 if is_selected else 3,
                dash_array=None if is_selected else "6 6",
                tooltip=f"#{overlap['rank']} {overlap.tier}: {overlap.distance_km} km",
            ).add_to(map_view)
        map_view.fit_bounds([selected.link[0], selected.link[1]], max_zoom=10)
        st_folium(map_view, height=520, use_container_width=True, returned_objects=[])
        st.caption(
            f"Blue: {utility_a} · Green: {utility_b} · "
            "dashed links show other ranked opportunities."
        )

    with right:
        st.subheader(f"Opportunity #{selected['rank']}")
        st.write(f"**{selected.project_a}** · {selected.name_a}")
        st.write(f"**{selected.project_b}** · {selected.name_b}")
        st.write(f"Closest distance: **{selected.distance_km} km** ({selected.tier})")
        st.write(f"Timeline: **{selected.timeline}**")
        st.write(f"**Coordination angle:** {selected.share}")

        estimate = cost_estimate(projects, selected, row_width_m, land_cost, mobilization)
        st.subheader("Estimated savings")
        st.write(f"Shared corridor: {estimate['shared_corridor_km']} km")
        st.write(f"Land: ${estimate['land_savings']:,}")
        st.write(f"Crews and equipment: ${estimate['crew_savings']:,}")
        st.metric("Total opportunity value", f"${estimate['total']:,}")
        st.caption("A directional estimate based on the assumptions in the sidebar.")

        opportunity_key = "|".join(
            sorted((str(selected.project_a), str(selected.project_b)))
        )
        if st.button(
            "Add to coordination pipeline",
            type="primary",
            disabled=opportunity_key in saved_keys,
        ):
            save_opportunity(engine, selected, estimate)
            st.rerun()

        with st.expander("Draft a coordination brief"):
            if st.button("Generate brief", key="generate_brief"):
                api_key = os.getenv("GEMINI_API_KEY")
                if not api_key:
                    st.error("Set GEMINI_API_KEY to generate a brief.")
                else:
                    from google import genai

                    prompt = (
                        "Write a concise coordination brief for two electric utility "
                        "planning teams. State what overlaps, what they could share, "
                        "the timeline, and one concrete next step. Keep it under 120 words.\n"
                        f"Project A: {selected.project_a} {selected.name_a}\n"
                        f"Project B: {selected.project_b} {selected.name_b}\n"
                        f"Distance: {selected.distance_km} km ({selected.tier}). "
                        f"Timeline: {selected.timeline}. Shareable: {selected.share}. "
                        f"Estimated savings: ${estimate['total']:,}."
                    )
                    with st.spinner("Drafting brief..."):
                        client = genai.Client(api_key=api_key)
                        response = client.models.generate_content(
                            model=GEMINI_MODEL,
                            contents=prompt,
                        )
                    st.write(response.text)

    st.subheader("Ranked coordination opportunities")
    st.dataframe(
        overlaps[
            [
                "rank",
                "project_a",
                "name_a",
                "project_b",
                "name_b",
                "distance_km",
                "tier",
                "timeline",
                "score",
            ]
        ],
        hide_index=True,
        width="stretch",
    )
    with st.expander("How ranking works"):
        for max_km, tier_name, share, _, points in TIERS:
            st.write(f"**{tier_name}** (up to {max_km:g} km, {points} pts): {share}")
        st.write(
            "Geographic distance sets the primary tier. A shared build year "
            "window adds 20 points; projects with no shared years receive no "
            "timeline bonus. Closer projects rank higher within each tier."
        )

st.subheader("Coordination pipeline")
if opportunities.empty:
    st.caption("Save an opportunity above to start tracking outreach and coordination.")
else:
    pipeline_columns = st.columns([3, 1, 1])
    pipeline_columns[0].dataframe(
        opportunities[
            [
                "project_a_name",
                "project_b_name",
                "distance_km",
                "estimated_savings",
                "status",
                "updated_at",
            ]
        ].rename(
            columns={
                "project_a_name": "Project A",
                "project_b_name": "Project B",
                "distance_km": "Distance (km)",
                "estimated_savings": "Est. savings ($)",
                "status": "Status",
                "updated_at": "Last updated",
            }
        ),
        hide_index=True,
        width="stretch",
    )
    option_labels = {
        row.opportunity_key: (
            f"{row.project_a_name} ↔ {row.project_b_name} · {row.status}"
        )
        for row in opportunities.itertuples()
    }
    selected_key = pipeline_columns[1].selectbox(
        "Update lead",
        option_labels,
        format_func=option_labels.get,
    )
    new_status = pipeline_columns[2].selectbox(
        "New status",
        PIPELINE_STATUSES,
        key="pipeline_status",
    )
    if st.button("Update status", disabled=opportunities.set_index(
        "opportunity_key"
    ).loc[selected_key, "status"] == new_status):
        update_opportunity_status(engine, selected_key, new_status)
        st.rerun()

events = load_recent_events(engine)
with st.expander("Recent coordination activity"):
    if events.empty:
        st.caption("Pipeline actions will appear here as a timestamped activity stream.")
    else:
        st.dataframe(
            events[
                [
                    "event_time",
                    "project_a_name",
                    "project_b_name",
                    "event_type",
                    "status",
                    "estimated_savings",
                ]
            ].rename(
                columns={
                    "event_time": "Time",
                    "project_a_name": "Project A",
                    "project_b_name": "Project B",
                    "event_type": "Action",
                    "status": "Status",
                    "estimated_savings": "Est. savings ($)",
                }
            ),
            hide_index=True,
            width="stretch",
        )
