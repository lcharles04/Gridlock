"""GridLock overlap engine.

Loads planned projects, measures closest-point distance between every
cross-utility pair, assigns Sperry's distance tiers, checks timeline
overlap, and ranks coordination opportunities.
"""
import math

import pandas as pd
from shapely.errors import GEOSException
from shapely import wkt
from shapely.ops import transform, nearest_points
from pyproj import Transformer

# UTM zone 17N covers South Carolina and Georgia. Distances in meters.
_TO_METERS = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True).transform
_TO_LATLON = Transformer.from_crs("EPSG:32617", "EPSG:4326", always_xy=True).transform

TOUCH_KM = 0.01  # within 10 m counts as touching / crossing

# (max_km, tier name, what can be shared, map color, base score)
TIERS = [
    (TOUCH_KM, "Touching / crossing", "Must coordinate outage timing and crossing structures", "#8B1A1A", 100),
    (1.6, "Under 1.6 km", "Share right-of-way, access roads, and permits", "#D2452B", 75),
    (8.0, "Under 8 km", "Share laydown yards and deliveries", "#E8893A", 50),
    (40.0, "Under 40 km", "Share crews and equipment", "#E9C46A", 25),
]


def load_projects(source) -> pd.DataFrame:
    """Read a CSV with columns: id, utility, name, kind, voltage_kv,
    start_year, end_year, geometry_wkt (lon/lat)."""
    try:
        df = source.copy() if isinstance(source, pd.DataFrame) else pd.read_csv(source)
    except pd.errors.ParserError as error:
        raise ValueError("The project CSV could not be parsed.") from error
    required = {
        "id", "utility", "name", "kind", "voltage_kv",
        "start_year", "end_year", "geometry_wkt",
    }
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(missing)}")
    if df.empty:
        raise ValueError("The project CSV has no project rows.")

    for column in ("id", "utility", "name", "kind", "geometry_wkt"):
        df[column] = df[column].astype("string").str.strip()
        if df[column].isna().any() or df[column].eq("").any():
            raise ValueError(f"Every project must have a non-empty {column}.")
    if df["id"].duplicated().any():
        raise ValueError("Project IDs must be unique.")
    if not df["kind"].isin(["line", "substation"]).all():
        raise ValueError("Project kind must be 'line' or 'substation'.")

    numeric_columns = ("voltage_kv", "start_year", "end_year")
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="raise")
        if df[column].isna().any() or not df[column].apply(math.isfinite).all():
            raise ValueError(f"Every project must have a finite number for {column}.")
    if (df["voltage_kv"] <= 0).any():
        raise ValueError("Voltage must be greater than zero.")
    if (df[["voltage_kv", "start_year", "end_year"]] % 1 != 0).any().any():
        raise ValueError("Voltage and project years must be whole numbers.")
    if (df["end_year"] < df["start_year"]).any():
        raise ValueError("A project's end year cannot be before its start year.")
    df[list(numeric_columns)] = df[list(numeric_columns)].astype("int64")

    try:
        df["geom"] = df["geometry_wkt"].apply(wkt.loads)
    except GEOSException as error:
        raise ValueError("Project geometry must contain valid WKT.") from error
    if not df["geom"].apply(
        lambda geometry: not geometry.is_empty
        and geometry.geom_type in {"Point", "LineString"}
    ).all():
        raise ValueError("Project geometry must be a non-empty POINT or LINESTRING.")
    if not df["geom"].apply(
        lambda geometry: (
            -180 <= geometry.bounds[0] <= geometry.bounds[2] <= 180
            and -90 <= geometry.bounds[1] <= geometry.bounds[3] <= 90
        )
    ).all():
        raise ValueError("Project geometry coordinates must be longitude/latitude.")
    df["geom_m"] = df["geom"].apply(lambda g: transform(_TO_METERS, g))
    return df


def get_tier(distance_km: float):
    if distance_km <= TOUCH_KM:
        return TIERS[0]
    if distance_km < 1.6:
        return TIERS[1]
    if distance_km < 8.0:
        return TIERS[2]
    if distance_km <= 40.0:
        return TIERS[3]
    return None


def timeline_overlap(a, b):
    """Return (overlap_years, gap_years). Gap is 0 when windows overlap."""
    start, end = max(a.start_year, b.start_year), min(a.end_year, b.end_year)
    if start <= end:
        return end - start + 1, 0
    return 0, start - end


def find_overlaps(df: pd.DataFrame, utility_a: str, utility_b: str) -> pd.DataFrame:
    a_rows = df[df.utility == utility_a]
    b_rows = df[df.utility == utility_b]
    results = []
    for _, a in a_rows.iterrows():
        for _, b in b_rows.iterrows():
            dist_km = a.geom_m.distance(b.geom_m) / 1000
            tier = get_tier(dist_km)
            if tier is None:
                continue
            overlap_yrs, gap_yrs = timeline_overlap(a, b)
            timeline_bonus = 20 if overlap_yrs else 0
            score = tier[4] + timeline_bonus - dist_km * 0.25
            pa, pb = nearest_points(a.geom_m, b.geom_m)
            pa, pb = transform(_TO_LATLON, pa), transform(_TO_LATLON, pb)
            results.append({
                "project_a": a.id, "name_a": a["name"],
                "project_b": b.id, "name_b": b["name"],
                "distance_km": round(dist_km, 2),
                "tier": tier[1], "share": tier[2], "color": tier[3],
                "timeline": (
                    f"{overlap_yrs} yr overlap"
                    if overlap_yrs
                    else f"No overlap ({gap_yrs} yr gap)"
                ),
                "overlap_years": overlap_yrs,
                "score": round(score, 1),
                "link": [(pa.y, pa.x), (pb.y, pb.x)],
            })
    out = pd.DataFrame(results)
    if out.empty:
        return out
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    out.insert(0, "rank", out.index + 1)
    return out


def cost_estimate(df, row, row_width_m, land_cost_per_acre, mobilization_savings):
    """Rough savings for one opportunity. Assumptions are user-set sliders."""
    a = df[df.id == row.project_a].iloc[0]
    b = df[df.id == row.project_b].iloc[0]
    shared_length_m = 0.0
    land_savings = 0.0
    if row.distance_km < 1.6 and a.kind == "line" and b.kind == "line":
        # Length of line A running within 1.6 km of line B = candidate shared corridor
        shared_length_m = a.geom_m.intersection(b.geom_m.buffer(1600)).length
        acres = shared_length_m * row_width_m / 4046.86
        land_savings = acres * land_cost_per_acre
    crew_savings = mobilization_savings if row.overlap_years else 0
    return {
        "shared_corridor_km": round(shared_length_m / 1000, 2),
        "land_savings": round(land_savings),
        "crew_savings": round(crew_savings),
        "total": round(land_savings + crew_savings),
    }
