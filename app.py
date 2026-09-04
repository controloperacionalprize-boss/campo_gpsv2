import streamlit as st
import zipfile
from io import BytesIO
import geopandas as gpd
import tempfile
import folium
import streamlit.components.v1 as components
import pandas as pd
import re
import requests
import json
import hashlib
import msal

st.set_page_config(
    page_title="Rendimiento de Cultivo",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
        [data-testid="stHeader"] { display: none; }
        .block-container {
            padding-top: 0.5rem;
            padding-left: 1rem;
            padding-right: 1rem;
            padding-bottom: 0 !important;
            max-width: 100% !important;
        }
        iframe {
            width: 100% !important;
            height: calc(100vh - 60px) !important;
            display: block;
            border: none;
        }
        h1 { font-size: 1.4rem !important; }
        [data-testid="stFileUploader"] { width: 100%; }
    </style>
""", unsafe_allow_html=True)

def parse_description(html):
    pairs = re.findall(r'<td>([^<]+)</td>\s*<td>([^<]+)</td>', str(html))
    return {k.strip(): v.strip() for k, v in pairs}

def limpiar_nombre_modulo(nombre):
    nombre = str(nombre)
    prefijo = ''
    prefijo_match = re.match(r'(AQ\d+)\s*-\s*', nombre, re.IGNORECASE)
    if prefijo_match:
        prefijo = prefijo_match.group(1).upper() + ' - '
    match = re.search(r'MODULO\s+0*(\d+)', nombre, re.IGNORECASE)
    if match:
        mod_num = str(int(match.group(1))).zfill(2)
        return f"{prefijo}MODULO {mod_num}"
    return nombre

PALETA = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9a6324", "#615f4a", "#800000", "#aaffc3",
    "#808000", "#ffd8b1", "#000075", "#a9a9a9", "#ff4444",
    "#44ff44", "#4444ff", "#ffaa00", "#aa00ff", "#00aaff",
]

def hash_bytes(data):
    return hashlib.md5(data).hexdigest()

def mapear_placemark_a_carpeta(kml_path):
    import xml.etree.ElementTree as ET
    mapping = {}
    try:
        tree = ET.parse(kml_path)
        root = tree.getroot()
        ns_uri = root.tag.split('}')[0].lstrip('{') if '}' in root.tag else ''
        ns = {'k': ns_uri} if ns_uri else {}
        tag = lambda t: f"k:{t}" if ns else t
        for folder in root.iter(f"{'{' + ns_uri + '}' if ns_uri else ''}Folder"):
            name_el = folder.find(tag('name'), ns)
            if name_el is None or not name_el.text:
                continue
            carpeta = name_el.text.strip()
            for pm in folder.findall(tag('Placemark'), ns):
                pm_name = pm.find(tag('name'), ns)
                if pm_name is not None and pm_name.text:
                    mapping[pm_name.text.strip()] = carpeta
    except Exception:
        pass
    return mapping

@st.cache_data(show_spinner="Procesando KMZ...")
def procesar_kmz(file_bytes, file_hash):
    kml_bytes = None
    with zipfile.ZipFile(BytesIO(file_bytes), 'r') as z:
        for nombre in z.namelist():
            if nombre.endswith('.kml'):
                kml_bytes = z.read(nombre)
                break
    if kml_bytes is None:
        return None, None, {}

    with tempfile.NamedTemporaryFile(delete=False, suffix='.kml', mode='wb') as tmp:
        tmp.write(kml_bytes)
        kml_path = tmp.name

    gdfs = []
    try:
        import pyogrio
        capas = [r[0] for r in pyogrio.list_layers(kml_path)]
    except Exception:
        capas = []

    for capa in capas:
        try:
            gdf_capa = gpd.read_file(kml_path, layer=capa, engine='pyogrio')
            if not gdf_capa.empty:
                gdf_capa['capa'] = capa
                gdfs.append(gdf_capa)
        except Exception:
            continue

    if not gdfs:
        return None, None, {}

    gdf = pd.concat(gdfs, ignore_index=True)
    gdf_valid = gdf[gdf.geometry.notna()].copy()
    gdf_valid = gdf_valid[~gdf_valid['capa'].str.contains('Pozos', case=False, na=False)]

    gdf_pol = gdf_valid[gdf_valid.geometry.geom_type.isin(['Polygon', 'MultiPolygon'])].copy().reset_index(drop=True)
    gdf_pts = gdf_valid[gdf_valid.geometry.geom_type == 'Point'].copy().reset_index(drop=True)

    gdf_pol = gdf_pol[~gdf_pol.geometry.is_empty]
    gdf_pol = gdf_pol[gdf_pol.geometry.is_valid].reset_index(drop=True)

    capas_unicas = list(dict.fromkeys(gdf_valid['capa'].tolist()))
    color_por_capa = {capa: PALETA[i % len(PALETA)] for i, capa in enumerate(capas_unicas)}

    nombre_a_carpeta = mapear_placemark_a_carpeta(kml_path)

    gdf_pol['modulo_raw'] = gdf_pol['Name'].map(nombre_a_carpeta).fillna(gdf_pol['capa'])
    gdf_pol['color_mod']  = gdf_pol['modulo_raw'].map(color_por_capa).fillna(gdf_pol['capa'].map(color_por_capa))
    gdf_pol['modulo']     = gdf_pol['modulo_raw'].apply(limpiar_nombre_modulo)
    parsed = gdf_pol['description'].apply(parse_description)
    gdf_pol['Turno'] = parsed.apply(lambda x: x.get('Turno', ''))
    gdf_pol['Lote']  = parsed.apply(lambda x: x.get('Lote', ''))
    gdf_pol['Area']  = parsed.apply(lambda x: x.get('Area', ''))

    return gdf_pol, gdf_pts, color_por_capa


def construir_geojson_lotes(gdf_pol):
    gdf_export = gdf_pol[['modulo', 'Turno', 'Lote', 'Area', 'color_mod', 'geometry']].copy()
    return gdf_export.to_json()


def construir_modulos_summary(gdf_pol):
    from shapely.ops import unary_union
    rows = []
    for modulo, group in gdf_pol.groupby('modulo'):
        try:
            geoms_validas = group.geometry[group.geometry.notna() & group.geometry.is_valid]
            if geoms_validas.empty:
                continue
            union_geom = unary_union(geoms_validas)
            centroid = union_geom.centroid
            if centroid.is_empty or pd.isna(centroid.x):
                centroid = geoms_validas.iloc[0].centroid
        except Exception:
            try:
                centroid = group.geometry.dropna().iloc[0].centroid
            except Exception:
                continue
        color = group['color_mod'].dropna().iloc[0] if group['color_mod'].notna().any() else '#aaaaaa'
        turnos = [
            {'turno': str(r.get('Turno', '')), 'lote': str(r.get('Lote', '')), 'area': str(r.get('Area', ''))}
            for _, r in group.iterrows()
        ]
        rows.append({'modulo': modulo, 'lat': centroid.y, 'lng': centroid.x, 'color': color, 'turnos': turnos})
    return json.dumps(rows, ensure_ascii=False)


def construir_turnos_summary(gdf_pol):
    """Centroide por combinación única (modulo, Turno) para los badges de zoom cercano."""
    from shapely.ops import unary_union
    rows = []
    for (modulo, turno), group in gdf_pol.groupby(['modulo', 'Turno']):
        if not turno:
            continue
        try:
            geoms_validas = group.geometry[group.geometry.notna() & group.geometry.is_valid]
            if geoms_validas.empty:
                continue
            union_geom = unary_union(geoms_validas)
            centroid = union_geom.centroid
            if centroid.is_empty or pd.isna(centroid.x):
                centroid = geoms_validas.iloc[0].centroid
        except Exception:
            try:
                centroid = group.geometry.dropna().iloc[0].centroid
            except Exception:
                continue
        color = group['color_mod'].dropna().iloc[0] if group['color_mod'].notna().any() else '#aaaaaa'

        turno_str = turno.strip()
        import re as _re
        num_match = _re.fullmatch(r'0*(\d+)', turno_str)
        if num_match:
            turno_label = 'Turno ' + num_match.group(1).zfill(2)
        else:
            turno_label = 'Turno ' + turno_str

        rows.append({
            'modulo':      modulo,
            'turno':       turno,
            'turno_label': turno_label,
            'lat':         centroid.y,
            'lng':         centroid.x,
            'color':       color,
        })
    return json.dumps(rows, ensure_ascii=False)


# ── SharePoint auth — device flow (igual que el bot) ─────────────────────────
_SP_CLIENT_ID = "d3590ed6-52b3-4102-aeff-aad2292ab01c"
_SP_AUTHORITY = "https://login.microsoftonline.com/common"
_SP_SCOPES    = ["https://aquanqape.sharepoint.com/.default"]

# (site_base, server_relative_path)
_SP_TURNOS = (
    "https://aquanqape.sharepoint.com/sites/DOCUMENTOSAREAAPLICATIVOS",
    "/sites/DOCUMENTOSAREAAPLICATIVOS/Documentos compartidos/OneDrive_1_28-7-2026/vista_kmz_turnos_2025_2026.xlsx",
)
_SP_PESOB = (
    "https://aquanqape.sharepoint.com/sites/OficinasPrizePeru",
    "/sites/OficinasPrizePeru/Documentos compartidos/PowerBI Global/04.-BI-Division Administrativo/11.-Control Operacional/9.-Peso Baya Elifab/PESO_BAYA_ELIFAT.xlsx",
)
_SP_PODA = (
    "https://aquanqape-my.sharepoint.com/personal/ccoz_aquanqa_pe",
    "/personal/ccoz_aquanqa_pe/Documents/DATALAKE_COZ/PODA/PODA_BI_PYTHON.xlsx",
)

_sp_token_cache = msal.SerializableTokenCache()

@st.cache_resource
def _get_sp_app():
    return msal.PublicClientApplication(
        client_id=_SP_CLIENT_ID,
        authority=_SP_AUTHORITY,
        token_cache=_sp_token_cache,
    )

_SP_SCOPES_MY = ["https://aquanqape-my.sharepoint.com/.default"]

def _get_token_for(scopes: list[str]) -> str | None:
    app      = _get_sp_app()
    accounts = app.get_accounts()
    result   = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if result and "access_token" in result:
        return result["access_token"]

    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        st.error("No se pudo iniciar el flujo de autenticación.")
        st.stop()

    st.info(
        f"**Autenticación requerida** — Ve a "
        f"[microsoft.com/devicelogin](https://microsoft.com/devicelogin) "
        f"e ingresa el código: **`{flow['user_code']}`**"
    )
    with st.spinner("Esperando autenticación... (recarga la página una vez que ingreses el código)"):
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        st.error(f"Error de autenticación: {result.get('error_description', result)}")
        st.stop()
    return result["access_token"]

def _autenticar_todo():
    """Autentica ambos scopes de una vez al inicio."""
    _get_token_for(_SP_SCOPES)
    _get_token_for(_SP_SCOPES_MY)

def _get_sp_token() -> str | None:
    return _get_token_for(_SP_SCOPES)

def _get_sp_token_my() -> str | None:
    return _get_token_for(_SP_SCOPES_MY)

def _descargar_excel(site_base: str, ruta: str, label: str, token_fn=None) -> pd.DataFrame:
    try:
        token = (token_fn or _get_sp_token)()
        url   = f"{site_base}/_api/web/GetFileByServerRelativeUrl('{ruta}')/$value"
        resp  = requests.get(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
        }, timeout=30, allow_redirects=True)
        if resp.status_code != 200:
            st.warning(f"No se pudo descargar {label}: HTTP {resp.status_code}")
            return pd.DataFrame()
        df = pd.read_excel(BytesIO(resp.content)).dropna(how="all")
        df.columns = [str(c).strip() for c in df.columns]
        return df
    except Exception as e:
        st.warning(f"Error al cargar {label}: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600, show_spinner="Descargando datos de rendimiento...")
def _cargar_df_turnos() -> pd.DataFrame:
    return _descargar_excel(*_SP_TURNOS, "vista_kmz_turnos")

@st.cache_data(ttl=600, show_spinner="Descargando datos de peso baya...")
def _cargar_df_peso_baya() -> pd.DataFrame:
    return _descargar_excel(*_SP_PESOB, "PESO_BAYA_ELIFAT")

@st.cache_data(ttl=600, show_spinner="Descargando datos de plantas...")
def _cargar_df_poda() -> pd.DataFrame:
    return _descargar_excel(*_SP_PODA, "PODA_BI_PYTHON", token_fn=_get_sp_token_my)


_MODULO_ALIAS = {
    "QURI ALLPA": "VIVADIS", "KAWSAY ALLPA": "SANTA TERESA",
    "AQU ANQA - ARENAAZUL": "ARENA AZUL",
}

# Mapeo de desc_productor (raw.all_recepciones) → fundo corto usado en el mapa
_PRODUCTOR_A_FUNDO = {
    "AQU ANQA S.A.C":    "AQ1",
    "AQU ANQA II S.A.C": "AQ2",
    "AQUA":              "AQ1",
    "AQUA II":           "AQ2",
}

# Fundos internos de cada empresa (recepciones solo tiene empresa, no fundo)
_FUNDO_A_EMPRESA = {
    "AYLLU ALLPA":  "AQ2",
    "SANTA TERESA": "AQ2",
    "VIVADIS":      "AQ2",
    "ARENA AZUL":   "AQ1",
}

def _norm_fundo(f: str) -> str:
    key = f.upper().strip()
    if key in _PRODUCTOR_A_FUNDO:
        return _PRODUCTOR_A_FUNDO[key]
    return _MODULO_ALIAS.get(key, key)

def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def _norm_modulo(m: str) -> str:
    import re as _re
    m = _strip_accents(m.upper().strip())
    # MD02 → MODULO 02, MD10B → MODULO 10
    md = _re.match(r"MD\s*0*(\d+)[A-Z]?$", m)
    if md:
        m = f"MODULO {int(md.group(1)):02d}"
    for alias in ("MODULO 10-A", "MODULO 10-B", "MODULO 10 A", "MODULO 10 B"):
        if m == alias:
            return "MODULO 10"
    match = _re.match(r"MODULO\s+0*(\d+)$", m)
    if match:
        return f"MODULO {int(match.group(1)):02d}"
    return m

@st.cache_data(ttl=600, show_spinner="Cargando kilos reales por módulo...")
def _cargar_total_kilos() -> dict:
    """Devuelve {(anio, fundo, modulo): total_kilos} desde raw.all_recepciones (Peru)"""
    import psycopg2
    cfg = st.secrets["warehouse"]
    try:
        conn = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
            user=cfg["user"], password=cfg["password"], sslmode="require",
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT
                EXTRACT(YEAR FROM fecha_recepcion)::int AS anio,
                cod_empresa,
                desc_cuartel_sector,
                SUM(kilos) AS total_kilos
            FROM raw.all_recepciones
            WHERE pais_origen_fuente = 'Peru'
              AND kilos IS NOT NULL
              AND fecha_recepcion IS NOT NULL
            GROUP BY 1, 2, 3
        """)
        result = {}
        for anio, fundo, modulo, kilos in cur.fetchall():
            f = _norm_fundo(str(fundo or ""))
            m = _norm_modulo(str(modulo or ""))
            result[(int(anio), f, m)] = round(float(kilos or 0), 2)
        cur.close()
        conn.close()
        return result
    except Exception as e:
        st.warning(f"No se pudo cargar kilos reales: {e}")
        return {}

@st.cache_data(ttl=600, show_spinner="Cargando proyectado y presupuestado...")
def _cargar_proy_ppto() -> dict:
    """Devuelve {(anio, fundo, modulo): {"kg_proy": float, "kg_ppto": float}}"""
    import psycopg2, ssl
    cfg = st.secrets["warehouse"]
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
            user=cfg["user"], password=cfg["password"], sslmode="require",
        )
        cur = conn.cursor()
        result = {}

        for tabla, campo in [
            ("raw.pe_xlsx_bd_proy_2026", "kg_proy"),
            ("raw.pe_xlsx_bd_ppto_2026", "kg_ppto"),
        ]:
            cur.execute(f"SELECT fecha, fundo, modulo, kilos FROM {tabla}")
            for fecha, fundo, modulo, kilos in cur.fetchall():
                anio   = fecha.year if hasattr(fecha, "year") else int(str(fecha)[:4])
                fundo  = _norm_fundo(str(fundo or ""))
                modulo = _norm_modulo(str(modulo or ""))
                key    = (anio, fundo, modulo)
                if key not in result:
                    result[key] = {"kg_proy": 0.0, "kg_ppto": 0.0}
                result[key][campo] = round(float(kilos or 0), 2)

        cur.close()
        conn.close()
        return result
    except Exception as e:
        st.warning(f"No se pudo cargar proyectado/presupuestado: {e}")
        return {}

def _col(df: pd.DataFrame, *candidatos) -> str | None:
    """Devuelve el primer nombre de columna que coincida (case-insensitive)."""
    mapa = {c.upper(): c for c in df.columns}
    for cand in candidatos:
        if cand.upper() in mapa:
            return mapa[cand.upper()]
    return None


@st.cache_data(ttl=600, show_spinner="Cargando rendimiento módulos...")
def cargar_rendimiento_modulos() -> list:
    df_t = _cargar_df_turnos()
    df_b = _cargar_df_peso_baya()
    df_p = _cargar_df_poda()

    # ── Turno: prom_jarras por año/fundo/módulo ──
    jarras_map = {}
    if not df_t.empty:
        c_fecha  = _col(df_t, "FECHA")
        c_fundo  = _col(df_t, "FUNDO")
        c_modulo = _col(df_t, "MODULO")
        c_jarras = _col(df_t, "prom_jarras")
        if all([c_fecha, c_fundo, c_modulo, c_jarras]):
            dt = df_t.copy()
            dt["_anio"]   = pd.to_datetime(dt[c_fecha], errors="coerce").dt.year
            dt["_fundo"]  = dt[c_fundo].astype(str).apply(_norm_fundo)
            dt["_modulo"] = dt[c_modulo].astype(str).apply(_norm_modulo)
            for (anio, fundo, modulo), grp in dt.dropna(subset=["_anio"]).groupby(["_anio", "_fundo", "_modulo"]):
                jarras_map[(int(anio), fundo, modulo)] = round(float(grp[c_jarras].mean()), 2)

    # ── Peso baya: total_kilos y peso_baya por año/productor/módulo ──
    baya_map = {}
    if not df_b.empty:
        c_fecha  = _col(df_b, "Fecha Cosecha")
        c_prod   = _col(df_b, "Productor")
        c_modulo = _col(df_b, "Módulo", "Modulo")
        c_kg     = _col(df_b, "Peso total (kg)")
        c_cnt    = _col(df_b, "Recuento")
        if all([c_fecha, c_prod, c_modulo, c_kg]):
            db = df_b.copy()
            db["_anio"]   = pd.to_datetime(db[c_fecha], errors="coerce").dt.year
            db["_fundo"]  = db[c_prod].astype(str).apply(_norm_fundo)
            db["_modulo"] = db[c_modulo].astype(str).apply(_norm_modulo)
            db[c_kg]      = pd.to_numeric(db[c_kg], errors="coerce").fillna(0)
            if c_cnt:
                db[c_cnt] = pd.to_numeric(db[c_cnt], errors="coerce").fillna(0)
            for (anio, fundo, modulo), grp in db.dropna(subset=["_anio"]).groupby(["_anio", "_fundo", "_modulo"]):
                total_kg  = float(grp[c_kg].sum())
                total_cnt = float(grp[c_cnt].sum()) if c_cnt else 0
                peso_baya = round(total_cnt and (total_kg / total_cnt) * 1000, 2)  # gramos
                baya_map[(int(anio), fundo, modulo)] = {
                    "total_kilos": round(total_kg, 2),
                    "peso_baya":   peso_baya,
                }

    # ── Proyectado / Presupuestado ──
    proy_map = _cargar_proy_ppto()

    # ── Kilos reales (recepciones) ──
    kilos_map = _cargar_total_kilos()

    # ── Plantas (poda) ──
    plantas_map = {}
    if not df_p.empty:
        c_empresa = _col(df_p, "Empresa")
        c_fundo   = _col(df_p, "Fundo")
        c_modulo  = _col(df_p, "Modulo")
        c_plantas = _col(df_p, "N_Plantas")
        c_fecha_p = _col(df_p, "Fecha Poda", "FechaSiembra", "Fecha Siembra")
        if all([c_fundo, c_modulo, c_plantas]):
            dp = df_p.copy()
            dp[c_plantas] = pd.to_numeric(dp[c_plantas], errors="coerce").fillna(0)
            if c_fecha_p:
                dp["_anio"] = pd.to_datetime(dp[c_fecha_p], errors="coerce").dt.year
            else:
                dp["_anio"] = pd.NA
            dp["_fundo"]  = dp[c_fundo].astype(str).apply(_norm_fundo)
            dp["_modulo"] = dp[c_modulo].astype(str).apply(_norm_modulo)
            for (anio, fundo, modulo), grp in dp.dropna(subset=["_anio"]).groupby(["_anio", "_fundo", "_modulo"]):
                plantas_map[(int(anio), fundo, modulo)] = int(grp[c_plantas].sum())

    # ── Combinar ──
    claves = set(jarras_map) | set(baya_map) | set(proy_map) | set(kilos_map) | set(plantas_map)
    result = []
    for (anio, fundo, modulo) in sorted(claves):
        b = baya_map.get((anio, fundo, modulo), {})
        if not b:
            empresa = _FUNDO_A_EMPRESA.get(fundo.upper(), fundo)
            b = baya_map.get((anio, empresa, modulo), {}) or \
                baya_map.get((anio, empresa, _norm_modulo(modulo)), {})
        pr = proy_map.get((anio, fundo, modulo), {})
        if not pr:
            mod_norm = _norm_modulo(modulo)
            pr = proy_map.get((anio, fundo, mod_norm), {})
        tk = kilos_map.get((anio, fundo, modulo), 0.0)
        if not tk:
            mod_norm = _norm_modulo(modulo)
            tk = kilos_map.get((anio, fundo, mod_norm), 0.0)
        if not tk:
            empresa = _FUNDO_A_EMPRESA.get(fundo.upper(), None)
            if empresa:
                tk = kilos_map.get((anio, empresa, modulo), 0.0) or \
                     kilos_map.get((anio, empresa, _norm_modulo(modulo)), 0.0)
        plantas = plantas_map.get((anio, fundo, modulo), 0)
        if not plantas:
            plantas = plantas_map.get((anio, fundo, _norm_modulo(modulo)), 0)
        kg_planta = round(tk / plantas, 4) if plantas and tk else 0.0
        result.append({
            "anio":          anio,
            "fundo":         fundo,
            "modulo":        modulo,
            "total_kilos":   tk,
            "total_plantas": plantas,
            "kg_planta":     kg_planta,
            "peso_baya":     b.get("peso_baya",   0.0),
            "prom_jarras":   jarras_map.get((anio, fundo, modulo), 0.0),
            "kg_proy":       pr.get("kg_proy", 0.0),
            "kg_ppto":       pr.get("kg_ppto", 0.0),
        })
    return result


@st.cache_data(ttl=600, show_spinner="Cargando rendimiento turnos...")
def cargar_rendimiento_turnos() -> list:
    df = _cargar_df_turnos()
    if df.empty:
        return []
    c_fecha  = _col(df, "FECHA")
    c_fundo  = _col(df, "FUNDO")
    c_modulo = _col(df, "MODULO")
    c_turno  = _col(df, "TURNO")
    c_tjars  = _col(df, "total_jarras")
    c_ntrab  = _col(df, "NUM_TRABAJADORES")
    c_pjars  = _col(df, "prom_jarras")
    c_kgper  = _col(df, "kg_persona")
    if not all([c_fecha, c_fundo, c_modulo, c_turno]):
        return []
    df = df.copy()
    df["_anio"]   = pd.to_datetime(df[c_fecha], errors="coerce").dt.year
    df["_fundo"]  = df[c_fundo].astype(str).str.upper().str.strip()
    df["_modulo"] = df[c_modulo].astype(str).str.upper().str.strip()
    df["_turno"]  = df[c_turno].astype(str).str.upper().str.strip()
    agg_cols = {k: v for k, v in {
        "total_jarras": c_tjars, "NUM_TRABAJADORES": c_ntrab,
        "prom_jarras": c_pjars,  "kg_persona": c_kgper,
    }.items() if v}
    result = []
    for (anio, fundo, modulo, turno), grp in df.dropna(subset=["_anio"]).groupby(
        ["_anio", "_fundo", "_modulo", "_turno"]
    ):
        result.append({
            "anio":                int(anio),
            "fundo":               fundo,
            "modulo":              modulo,
            "turno":               turno,
            "total_jarras":        int(grp[c_tjars].sum())         if c_tjars else 0,
            "total_trabajadores":  int(grp[c_ntrab].sum())         if c_ntrab else 0,
            "jarras_promedio":     round(float(grp[c_pjars].mean()), 2) if c_pjars else 0.0,
            "kg_persona_promedio": round(float(grp[c_kgper].mean()), 2) if c_kgper else 0.0,
        })
    return result

@st.cache_data(show_spinner="Cargando archivo KMZ...")
def cargar_kmz_github():
    import os

    # Intento 1: archivo local (cuando el KMZ está en el repo)
    for ruta_local in ["MODULOS_PRIZE_PAIJAN.kmz", "data/MODULOS_PRIZE_PAIJAN.kmz"]:
        if os.path.exists(ruta_local):
            with open(ruta_local, "rb") as f:
                data = f.read()
            if data[:2] == b'PK':
                return data

    # Intento 2: GitHub
    token = st.secrets.get("GITHUB_TOKEN_KMZ", "")
    url = "https://api.github.com/repos/controloperacionalprize-boss/CAMPO_RENDIMIENTO/contents/MODULOS_PRIZE_PAIJAN.kmz"
    headers_raw  = {"Accept": "application/vnd.github.v3.raw"}
    headers_json = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers_raw["Authorization"]  = f"token {token}"
        headers_json["Authorization"] = f"token {token}"

    r = requests.get(url, headers=headers_raw, timeout=30)
    if r.status_code == 200 and r.content[:2] == b'PK':
        return r.content

    r2 = requests.get(url, headers=headers_json, timeout=30)
    if r2.status_code == 200:
        try:
            meta = r2.json()
            dl_url = meta.get("download_url")
            if dl_url:
                r3 = requests.get(dl_url, headers={"Authorization": f"token {token}"} if token else {}, timeout=60)
                if r3.status_code == 200 and r3.content[:2] == b'PK':
                    return r3.content
            import base64 as _b64
            if meta.get("encoding") == "base64" and meta.get("content"):
                return _b64.b64decode(meta["content"])
        except Exception:
            pass

    return {"error": f"HTTP {r.status_code} — {r.text[:300]}"}


# ── App ───────────────────────────────────────────────────────────────────────
st.title("🌱 Rendimiento de Cultivo")

_autenticar_todo()

file_bytes          = cargar_kmz_github()
rendimiento_modulos = cargar_rendimiento_modulos()
rendimiento_turnos  = cargar_rendimiento_turnos()
ALIAS_FUNDO = {
    "KAWSAY ALLPA": "SANTA TERESA"
}
def normalizar_fundo(nombre):
    n = nombre.upper().strip()
    return ALIAS_FUNDO.get(n, n)

# ── Sidebar: filtros ──────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🔍 Filtros")

    anios_disp = sorted(
        {r["anio"] for r in rendimiento_modulos} |
        {r["anio"] for r in rendimiento_turnos}
    )
    anio_sel = st.selectbox(
        "📅 Año",
        options=["Todos"] + [str(a) for a in anios_disp],
        index=0,
    )

    fundos_disp = sorted(
        {normalizar_fundo(r["fundo"]) for r in rendimiento_modulos} |
        {normalizar_fundo(r["fundo"]) for r in rendimiento_turnos}
    )
    fundo_sel = st.selectbox(
        "🏡 Fundo",
        options=["Todos"] + fundos_disp,
        index=0,
    )
    st.markdown("---")
    st.caption("📌 El fundo filtra los módulos visibles en el mapa. El año resalta la fila en el modal.")

anio_filtro  = None if anio_sel  == "Todos" else int(anio_sel)
fundo_filtro = None if fundo_sel == "Todos" else fundo_sel
anio_js      = anio_filtro if anio_filtro else 0
fundo_js     = fundo_filtro if fundo_filtro else ""

# ── Filtro de polígonos: prefijo AQ + números de módulo desde la BD ──────────
FUNDO_A_AQ = {
    "ARENA AZUL":   "AQ1",
    "AYLLU ALLPA":  "AQ2",
    "VIVADIS":      "AQ2",
    "SANTA TERESA": "AQ2",
}

def _nums_de_fundo(fundo, lista):
    """Números de módulo (int) que pertenecen a un fundo según la BD."""
    nums = set()
    for r in lista:
        if normalizar_fundo(r["fundo"]) == fundo:
            m = re.search(r'MODULO\s*(\d+)', r["modulo"], re.IGNORECASE)
            if m:
                nums.add(int(m.group(1)))
    return nums

prefijo_aq_fundo = FUNDO_A_AQ.get(fundo_filtro) if fundo_filtro else None
nums_mod_fundo   = (
    _nums_de_fundo(fundo_filtro, rendimiento_modulos) |
    _nums_de_fundo(fundo_filtro, rendimiento_turnos)
) if fundo_filtro else None

# ── Filtra datos JS por fundo ─────────────────────────────────────────────────
def _filtrar_rend(lista, fundo=None):
    if fundo:
        return [r for r in lista if normalizar_fundo(r["fundo"]) == fundo]
    return lista

rendimiento_mod_str   = json.dumps(_filtrar_rend(rendimiento_modulos, fundo_filtro), ensure_ascii=False)
rendimiento_turno_str = json.dumps(_filtrar_rend(rendimiento_turnos,  fundo_filtro), ensure_ascii=False)

if isinstance(file_bytes, dict) and "error" in file_bytes:
    st.error(f"No se pudo cargar el KMZ desde GitHub: {file_bytes['error']}")
    st.stop()

if file_bytes:
    file_hash = hash_bytes(file_bytes)
    gdf_pol_full, gdf_pts_full, color_por_capa = procesar_kmz(file_bytes, file_hash)

    if gdf_pol_full is not None:

        # ── Filtra polígonos: prefijo AQ correcto Y número de módulo en la BD ──
        if prefijo_aq_fundo is not None and nums_mod_fundo is not None:
            def _modulo_valido(nombre_modulo):
                n = str(nombre_modulo).upper().strip()
                if not n.startswith(prefijo_aq_fundo):
                    return False
                m = re.search(r'MODULO\s*(\d+)', n)
                return bool(m) and int(m.group(1)) in nums_mod_fundo

            mask    = gdf_pol_full['modulo'].apply(_modulo_valido)
            gdf_pol = gdf_pol_full[mask].copy().reset_index(drop=True)

            TODOS_AQ = ["AQ1", "AQ2", "AQ3", "AQ4", "AQ5"]
            gdf_pts = gdf_pts_full[gdf_pts_full['Name'].apply(
                lambda n: _modulo_valido(n) or
                          not any(str(n).upper().strip().startswith(aq) for aq in TODOS_AQ)
            )].copy().reset_index(drop=True)
        else:
            gdf_pol = gdf_pol_full
            gdf_pts = gdf_pts_full

        ref = gdf_pol if not gdf_pol.empty else gdf_pts
        if ref.empty:
            st.warning("No hay módulos para el fundo seleccionado.")
            st.stop()
        bounds = ref.total_bounds
        centro = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

        mapa = folium.Map(location=centro, zoom_start=14, tiles='Esri.WorldImagery')

        # ── Geolocalización ──
        from folium.plugins import LocateControl
        LocateControl(
            auto_start=False,
            position="topleft",
            strings={"title": "Ver mi ubicación"},
            fly_to=True,
            keep_current_zoom_level=False,
        ).add_to(mapa)

        # ── Polígonos ──
        if not gdf_pol.empty:
            folium.GeoJson(
                gdf_pol,
                name="lotes",
                style_function=lambda x: {
                    "fillColor": x['properties'].get('color_mod', '#aaaaaa'),
                    "color": "white",
                    "weight": 1.5,
                    "fillOpacity": 0.65,
                    "opacity": 1.0
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["modulo", "Turno", "Lote", "Area"],
                    aliases=["Módulo:", "Turno:", "Lote:", "Area (ha):"],
                    sticky=True,
                    style=(
                        "background-color: white; color: black; "
                        "font-weight: bold; font-size: 14px; "
                        "padding: 8px; border-radius: 6px;"
                    )
                )
            ).add_to(mapa)

        # ── Etiquetas de puntos ──
        if not gdf_pts.empty:
            for _, row in gdf_pts.iterrows():
                nombre = str(row.get('Name', ''))
                color  = color_por_capa.get(str(row.get('capa', '')), '#aaaaaa')
                pt     = row.geometry
                if pd.isna(pt.x) or pd.isna(pt.y):
                    continue
                folium.Marker(
                    location=[pt.y, pt.x],
                    icon=folium.DivIcon(
                        icon_size=(120, 30),
                        icon_anchor=(60, 15),
                        html=f"""<div style="
                            font-size:15px; font-weight:900; color:white;
                            background-color:{color}; padding:4px 10px;
                            border-radius:5px; border:2px solid white;
                            box-shadow:2px 2px 6px rgba(0,0,0,0.7);
                            text-shadow:1px 1px 3px rgba(0,0,0,0.9);
                            white-space:nowrap; pointer-events:none;
                        ">{nombre}</div>"""
                    )
                ).add_to(mapa)

        geojson_str = construir_geojson_lotes(gdf_pol)
        modulos_str = construir_modulos_summary(gdf_pol)
        turnos_str  = construir_turnos_summary(gdf_pol)

        ZOOM_TURNO = 16

        js_code = f"""
        <script src="https://cdn.jsdelivr.net/npm/@turf/turf@6/turf.min.js"></script>

        <div id="lote-overlay" onclick="if(event.target===this)cerrarModal()">
          <div id="lote-modal">
            <div class="lm-header">
              <div>
                <div class="lm-badge-tipo" id="lm-badge-tipo"></div>
                <div class="lm-titulo"     id="lm-titulo-mod"></div>
                <div class="lm-subtitulo"  id="lm-subtitulo"></div>
              </div>
              <button class="lm-close" onclick="cerrarModal()">&#10005;</button>
            </div>
            <div class="lm-body">
              <div id="lm-rend-tabla" style="padding:12px 14px;"></div>
            </div>
            <div class="lm-footer">
              <button class="lm-btn" onclick="cerrarModal()">Cerrar</button>
            </div>
          </div>
        </div>

        <style>
            *, *::before, *::after {{ box-sizing: border-box; }}
            html, body {{ margin:0; padding:0; width:100%; height:100%; overflow:hidden; }}
            .folium-map, [id^="map_"] {{ width:100%!important; height:100vh!important; min-height:400px; }}

            #lote-overlay {{
                display:none; position:fixed; inset:0;
                background:rgba(0,0,0,0.52);
                z-index:99999; justify-content:center; align-items:center;
            }}
            #lote-overlay.active {{ display:flex; }}
            #lote-modal {{
                background:#fff; border-radius:18px;
                width:min(620px,95vw); max-height:min(88vh,740px);
                display:flex; flex-direction:column;
                box-shadow:0 12px 48px rgba(0,0,0,0.38);
                font-family:'Segoe UI',Arial,sans-serif;
                overflow:hidden;
                animation:lmPop .2s cubic-bezier(.34,1.56,.64,1);
            }}
            @keyframes lmPop {{
                from {{ transform:scale(.82) translateY(18px); opacity:0; }}
                to   {{ transform:scale(1)   translateY(0);    opacity:1; }}
            }}
            .lm-header {{
                background:var(--lm-color,#2563eb); color:#fff;
                padding:14px 18px 12px;
                display:flex; justify-content:space-between; align-items:flex-start;
                flex-shrink:0;
            }}
            .lm-badge-tipo {{ font-size:.65rem; font-weight:800; letter-spacing:1.5px; text-transform:uppercase; opacity:.7; margin-bottom:2px; }}
            .lm-titulo     {{ font-size:clamp(.92rem,2.5vw,1.15rem); font-weight:800; letter-spacing:.3px; }}
            .lm-subtitulo  {{ font-size:clamp(.7rem,2vw,.8rem); opacity:.8; margin-top:3px; }}
            .lm-close {{
                background:rgba(255,255,255,0.2); border:none; color:#fff;
                width:28px; height:28px; border-radius:50%; font-size:1rem;
                cursor:pointer; display:flex; align-items:center; justify-content:center;
                flex-shrink:0; transition:background .15s;
            }}
            .lm-close:hover {{ background:rgba(255,255,255,0.38); }}
            .lm-body {{ overflow-y:auto; flex:1; }}
            .lm-footer {{ padding:10px 14px 14px; border-top:1px solid #e5e7eb; flex-shrink:0; }}
            .lm-btn {{
                width:100%; padding:11px;
                background:var(--lm-color,#2563eb); color:#fff; border:none;
                border-radius:10px; font-size:clamp(.85rem,2.5vw,.95rem); font-weight:700;
                cursor:pointer; transition:filter .15s;
            }}
            .lm-btn:hover {{ filter:brightness(1.1); }}

            .lm-tabla-scroll {{ overflow-x:auto; }}
            .lm-tabla {{ width:100%; border-collapse:collapse; font-size:clamp(.78rem,2vw,.88rem); }}
            .lm-tabla thead tr {{ background:#f3f4f6; }}
            .lm-tabla th {{
                padding:8px 12px; text-align:right; white-space:nowrap;
                color:#6b7280; font-size:.7rem; text-transform:uppercase;
                letter-spacing:.5px; border-bottom:2px solid #e5e7eb;
            }}
            .lm-tabla th:first-child {{ text-align:left; }}
            .lm-tabla td {{
                padding:8px 12px; text-align:right;
                color:#111827; font-weight:500;
                border-bottom:1px solid #f0f0f0; white-space:nowrap;
            }}
            .lm-tabla td:first-child {{ text-align:left; }}
            .lm-tabla tbody tr:hover {{ background:#f9fafb; }}
            .anio-cell {{ font-weight:800; color:var(--lm-color,#2563eb)!important; }}
            .kg-highlight {{ font-weight:800; font-size:1rem; }}

            .fundo-sep {{
                padding:8px 12px 4px; font-size:.7rem; font-weight:800;
                color:var(--lm-color,#2563eb); text-transform:uppercase; letter-spacing:.5px;
                border-top:2px solid #e5e7eb; margin-top:4px;
            }}
            .fundo-sep:first-child {{ border-top:none; margin-top:0; }}

            .mod-badge {{
                color:white; padding:5px 14px; border-radius:20px;
                font-size:clamp(11px,1.5vw,14px); font-weight:bold;
                border:2px solid white; box-shadow:0 2px 10px rgba(0,0,0,0.55);
                white-space:nowrap; text-align:center;
                pointer-events:auto; cursor:pointer; line-height:1.4;
                transition:transform .12s, box-shadow .12s;
            }}
            .turno-badge {{
                color:white; padding:4px 12px; border-radius:16px;
                font-size:clamp(10px,1.3vw,13px); font-weight:800;
                border:2px dashed rgba(255,255,255,0.85);
                box-shadow:0 2px 8px rgba(0,0,0,0.5);
                white-space:nowrap; text-align:center;
                pointer-events:auto; cursor:pointer; line-height:1.4;
                transition:transform .12s, box-shadow .12s;
            }}
            .mod-badge:hover, .turno-badge:hover {{ transform:scale(1.08); box-shadow:0 4px 18px rgba(0,0,0,0.7); }}

            #hud {{
                position:fixed; bottom:clamp(12px,3vh,24px); left:50%; transform:translateX(-50%);
                z-index:9999; background:rgba(0,0,0,0.82); color:white;
                padding:clamp(8px,1.5vh,12px) clamp(14px,3vw,22px);
                border-radius:14px; font-family:Arial,sans-serif;
                font-size:clamp(12px,1.5vw,15px);
                min-width:min(260px,80vw); max-width:min(420px,90vw);
                text-align:center; box-shadow:0 4px 16px rgba(0,0,0,0.5);
                pointer-events:auto; cursor:pointer; line-height:1.6;
            }}
            #hud .hud-titulo    {{ font-size:clamp(10px,1.2vw,12px); color:#aaa; text-transform:uppercase; letter-spacing:1px; }}
            #hud .hud-modulo    {{ font-size:clamp(14px,2vw,18px); font-weight:bold; color:#FFD600; }}
            #hud .hud-detalle   {{ font-size:clamp(11px,1.4vw,14px); color:#ddd; }}
            #hud .hud-distancia {{ font-size:clamp(11px,1.4vw,14px); color:#69F0AE; margin-top:2px; }}
            #toast {{
                position:fixed; top:clamp(12px,2vh,20px); left:50%; transform:translateX(-50%);
                z-index:10000; background:#1B5E20; color:white;
                padding:10px 22px; border-radius:20px; font-family:Arial,sans-serif;
                font-size:clamp(12px,1.5vw,15px); font-weight:bold;
                box-shadow:0 4px 16px rgba(0,0,0,0.5); display:none;
                text-align:center; white-space:nowrap;
            }}
        </style>

        <div id="hud" style="display:none;"><div class="hud-titulo">Ubicacion</div><div class="hud-modulo">Buscando GPS...</div></div>
        <div id="toast"></div>

        <script>
        /* ── Resize responsivo ── */
        (function() {{
            function resizeMapa() {{
                document.querySelectorAll('.folium-map,[id^="map_"]').forEach(function(d) {{
                    d.style.width  = '100%';
                    d.style.height = window.innerHeight + 'px';
                }});
                var m = Object.values(window).find(function(v) {{
                    return v && v._leaflet_id && v.invalidateSize;
                }});
                if (m) m.invalidateSize();
            }}
            window.addEventListener('resize', resizeMapa);
            window.addEventListener('load',   resizeMapa);
            [200, 600, 1200].forEach(function(t) {{ setTimeout(resizeMapa, t); }});
        }})();

        /* ── Datos globales ── */
        var _rendimientoMod   = {rendimiento_mod_str};
        var _rendimientoTurno = {rendimiento_turno_str};
        var _anioSel          = {anio_js};
        var _fundoSel         = "{fundo_js}";

        function puntoDentroConTolerancia(punto, feature, tol) {{
            tol = tol || 5;
            if (turf.booleanPointInPolygon(punto, feature)) return true;
            try {{
                var linea = turf.polygonToLine(feature);
                return turf.pointToLineDistance(punto, linea, {{units:'meters'}}) <= tol;
            }} catch(e) {{ return false; }}
        }}

        /* ── Modal: módulo ── */
        function abrirModalModulo(moduloFoco, colorFoco) {{
            document.getElementById('lote-modal').style.setProperty('--lm-color', colorFoco || '#2563eb');
            document.getElementById('lm-badge-tipo').textContent = 'Módulo';
            document.getElementById('lm-titulo-mod').textContent = moduloFoco || 'Módulo';
            document.getElementById('lm-subtitulo').textContent  = 'Rendimiento histórico';

            var contenedor = document.getElementById('lm-rend-tabla');
            var numMatch   = (moduloFoco||'').toUpperCase().match(/MODULO\\s*(\\d+)/);
            var numMod     = numMatch ? parseInt(numMatch[1],10) : null;
            var aqMatch    = (moduloFoco||'').toUpperCase().match(/^(AQ\\d+)/);
            var aqPref     = aqMatch ? aqMatch[1] : null;

            var historial = [];
            if (numMod !== null) {{
                var _AQ_FUNDOS_MAP = {{
                    'AQ1': ['ARENA AZUL'],
                    'AQ2': ['AYLLU ALLPA','VIVADIS','SANTA TERESA']
                }};
                var _fundosValidos = aqPref ? (_AQ_FUNDOS_MAP[aqPref] || []) : [];

                historial = _rendimientoMod.filter(function(r) {{
                    var rm = r.modulo.match(/MODULO\\s*(\\d+)/);
                    if (!rm || parseInt(rm[1],10) !== numMod) return false;
                    if (_fundoSel) return r.fundo === _fundoSel;
                    if (_fundosValidos.length) return _fundosValidos.indexOf(r.fundo) !== -1;
                    return true;
                }});
            }}
            if (!historial.length) {{
                contenedor.innerHTML = '<p style="color:#9ca3af;text-align:center;padding:24px 0;">Sin datos de cosecha para este módulo.</p>';
            }} else {{
                var porFundo = {{}};
                historial.forEach(function(r) {{ (porFundo[r.fundo]=porFundo[r.fundo]||[]).push(r); }});
                var fundoKeys = Object.keys(porFundo);
                var html = '<div class="lm-tabla-scroll">';
                fundoKeys.forEach(function(f) {{
                    if (fundoKeys.length > 1) html += '<div class="fundo-sep">' + f + '</div>';
                    html += '<table class="lm-tabla"><thead><tr>' +
                        '<th>Año</th><th>Real kg</th><th>Proyectado kg</th><th>Presupuestado kg</th><th>Plantas</th><th>kg/planta</th><th>Peso baya</th><th>Jarras prom</th>' +
                        '</tr></thead><tbody>';
                    porFundo[f].filter(function(r){{ return r.anio === 2026; }}).forEach(function(r) {{
                        var _hl = (_anioSel && r.anio === _anioSel) ? 'background:#fef9c3;font-weight:700;' : '';
                        var _proyCell = r.kg_proy
                            ? r.kg_proy.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})
                            : '<span style="color:#9ca3af">—</span>';
                        var _pptoCell = r.kg_ppto
                            ? r.kg_ppto.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})
                            : '<span style="color:#9ca3af">—</span>';
                        html += '<tr style="' + _hl + '">' +
                            '<td class="anio-cell">' + r.anio + '</td>' +
                            '<td>' + r.total_kilos.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}}) + '</td>' +
                            '<td style="color:#7c3aed;font-weight:600;">' + _proyCell + '</td>' +
                            '<td style="color:#0891b2;font-weight:600;">' + _pptoCell + '</td>' +
                            '<td>' + (r.total_plantas||0).toLocaleString() + '</td>' +
                            '<td>' + r.kg_planta + '</td>' +
                            '<td>' + r.peso_baya + ' gr</td>' +
                            '<td>' + r.prom_jarras + '</td>' +
                            '</tr>';
                    }});
                    html += '</tbody></table>';
                }});
                html += '</div>';
                contenedor.innerHTML = html;
            }}
            document.getElementById('lote-overlay').classList.add('active');
        }}

        /* ── Modal: turno ── */
        function abrirModalTurno(moduloFoco, turnoFoco, colorFoco, turnoLabel) {{
            turnoLabel = turnoLabel || turnoFoco;
            document.getElementById('lote-modal').style.setProperty('--lm-color', colorFoco || '#059669');
            document.getElementById('lm-badge-tipo').textContent = 'Turno · ' + (moduloFoco||'');
            document.getElementById('lm-titulo-mod').textContent = turnoLabel;
            document.getElementById('lm-subtitulo').textContent  = 'kg por persona — histórico anual';

            var contenedor = document.getElementById('lm-rend-tabla');
            var modNorm    = (moduloFoco||'').toUpperCase().trim();
            var turnoNorm  = (turnoFoco ||'').toUpperCase().trim();
            var numMatch   = modNorm.match(/MODULO\\s*(\\d+)/);
            var numMod     = numMatch ? parseInt(numMatch[1],10) : null;
            var aqMatch    = modNorm.match(/^(AQ\\d+)/);
            var aqPref     = aqMatch ? aqMatch[1] : null;

            var turnoNumKmz = null;
            var _tmKmz = turnoNorm.match(/(\\d+)/);
            if (_tmKmz) turnoNumKmz = parseInt(_tmKmz[1], 10);

            var historial = _rendimientoTurno.filter(function(r) {{
                var turnoNumBd = null;
                var _tmBd = (r.turno||'').match(/(\\d+)/);
                if (_tmBd) turnoNumBd = parseInt(_tmBd[1], 10);
                if (turnoNumKmz === null || turnoNumBd === null) return false;
                if (turnoNumBd !== turnoNumKmz) return false;
                if (numMod !== null) {{
                    var rm = (r.modulo||'').match(/MODULO\\s*(\\d+)/);
                    if (!rm || parseInt(rm[1],10) !== numMod) return false;
                }}
                if (_fundoSel) return r.fundo === _fundoSel;
                var _AQ_FM = {{'AQ1':['ARENA AZUL'],'AQ2':['AYLLU ALLPA','VIVADIS','SANTA TERESA']}};
                var _fv = aqPref ? (_AQ_FM[aqPref]||[]) : [];
                if (_fv.length) return _fv.indexOf(r.fundo) !== -1;
                return true;
            }});

            if (!historial.length) {{
                contenedor.innerHTML = '<p style="color:#9ca3af;text-align:center;padding:24px 0;">Sin datos históricos para este turno.</p>';
            }} else {{
                var porFundo = {{}};
                historial.forEach(function(r) {{ (porFundo[r.fundo]=porFundo[r.fundo]||[]).push(r); }});
                var fundoKeys = Object.keys(porFundo);
                var html = '<div class="lm-tabla-scroll">';
                fundoKeys.forEach(function(f) {{
                    if (fundoKeys.length > 1) html += '<div class="fundo-sep">' + f + '</div>';
                    html += '<table class="lm-tabla"><thead><tr>' +
                        '<th>Año</th><th>Total jarras</th><th>Trabajadores</th><th>Jarras/persona</th><th>kg/persona</th>' +
                        '</tr></thead><tbody>';
                    porFundo[f].filter(function(r){{ return r.anio === 2026; }}).forEach(function(r) {{
                        var _hl = (_anioSel && r.anio === _anioSel) ? 'background:#fef9c3;font-weight:700;' : '';
                        html += '<tr style="' + _hl + '">' +
                            '<td class="anio-cell">' + r.anio + '</td>' +
                            '<td>' + (r.total_jarras||0).toLocaleString() + '</td>' +
                            '<td>' + (r.total_trabajadores||0).toLocaleString() + '</td>' +
                            '<td>' + r.jarras_promedio.toFixed(2) + '</td>' +
                            '<td class="kg-highlight">' + r.kg_persona_promedio.toFixed(2) + ' kg</td>' +
                            '</tr>';
                    }});
                    html += '</tbody></table>';
                }});
                html += '</div>';
                contenedor.innerHTML = html;
            }}
            document.getElementById('lote-overlay').classList.add('active');
        }}

        function cerrarModal() {{
            document.getElementById('lote-overlay').classList.remove('active');
        }}
        document.addEventListener('keydown', function(e) {{ if (e.key==='Escape') cerrarModal(); }});

        /* ── Init mapa ── */
        (function() {{
            var modulosData = {modulos_str};
            var turnosData  = {turnos_str};
            var ZOOM_TURNO  = {ZOOM_TURNO};

            function initGeo() {{
                var mapEl = Object.values(window).find(function(v) {{
                    return v && v._leaflet_id && v.getCenter;
                }});
                if (!mapEl) {{ setTimeout(initGeo, 300); return; }}

                var lotesGeoJSON = {geojson_str};
                var hud          = document.getElementById('hud');
                var toast        = document.getElementById('toast');
                var moduloActual = null;
                var siguiendo    = true;
                var toastTimer   = null;
                var geoMarker    = null;
                var accuracyCircle = null;

                hud.addEventListener('click', function() {{
                    siguiendo = true;
                    if (geoMarker) mapEl.setView(geoMarker.getLatLng(), 17);
                }});

                /* ── Badges MÓDULO ── */
                var badgesModGroup = L.layerGroup();
                modulosData.forEach(function(m) {{
                    var mk = L.marker([m.lat, m.lng], {{
                        icon: L.divIcon({{
                            className: '',
                            html: '<div class="mod-badge" style="background:' + m.color + '">' + m.modulo + '</div>',
                            iconSize: [150,30], iconAnchor: [75,15]
                        }}),
                        interactive: true, zIndexOffset: 1000
                    }});
                    mk.on('click', function(e) {{
                        L.DomEvent.stopPropagation(e);
                        abrirModalModulo(m.modulo, m.color);
                    }});
                    badgesModGroup.addLayer(mk);
                }});

                /* ── Badges TURNO ── */
                var badgesTurnoGroup = L.layerGroup();
                turnosData.forEach(function(t) {{
                    var mk = L.marker([t.lat, t.lng], {{
                        icon: L.divIcon({{
                            className: '',
                            html: '<div class="turno-badge" style="background:' + t.color + '">' + (t.turno_label||t.turno) + '</div>',
                            iconSize: [130,28], iconAnchor: [65,14]
                        }}),
                        interactive: true, zIndexOffset: 1100
                    }});
                    mk.on('click', function(e) {{
                        L.DomEvent.stopPropagation(e);
                        abrirModalTurno(t.modulo, t.turno, t.color, t.turno_label);
                    }});
                    badgesTurnoGroup.addLayer(mk);
                }});

                /* ── Swap por zoom ── */
                function actualizarBadgesPorZoom() {{
                    var z = mapEl.getZoom();
                    if (z >= ZOOM_TURNO) {{
                        if (mapEl.hasLayer(badgesModGroup))    mapEl.removeLayer(badgesModGroup);
                        if (!mapEl.hasLayer(badgesTurnoGroup)) badgesTurnoGroup.addTo(mapEl);
                    }} else {{
                        if (mapEl.hasLayer(badgesTurnoGroup))  mapEl.removeLayer(badgesTurnoGroup);
                        if (!mapEl.hasLayer(badgesModGroup))   badgesModGroup.addTo(mapEl);
                    }}
                }}
                badgesModGroup.addTo(mapEl);
                mapEl.on('zoomend', actualizarBadgesPorZoom);

                /* ── GPS ── */
                var geoIcon = L.divIcon({{
                    className: '',
                    html: '<div style="width:18px;height:18px;background:#2979FF;border:3px solid white;border-radius:50%;box-shadow:0 0 0 3px rgba(41,121,255,0.4);"></div>',
                    iconSize: [18,18], iconAnchor: [9,9]
                }});

                function mostrarToast(texto) {{
                    toast.innerHTML = texto; toast.style.display = 'block';
                    if (toastTimer) clearTimeout(toastTimer);
                    toastTimer = setTimeout(function() {{ toast.style.display='none'; }}, 4000);
                }}

                function buscarLote(lat, lng) {{
                    var punto = turf.point([lng, lat]);
                    for (var i=0; i<lotesGeoJSON.features.length; i++) {{
                        try {{ if (puntoDentroConTolerancia(punto, lotesGeoJSON.features[i], 5)) return lotesGeoJSON.features[i].properties; }} catch(e) {{}}
                    }}
                    return null;
                }}

                function loteMasCercano(lat, lng) {{
                    var punto = turf.point([lng, lat]);
                    var minDist=Infinity, nearest=null;
                    for (var i=0; i<lotesGeoJSON.features.length; i++) {{
                        try {{
                            var d = turf.distance(punto, turf.centroid(lotesGeoJSON.features[i]), {{units:'meters'}});
                            if (d < minDist) {{ minDist=d; nearest={{props:lotesGeoJSON.features[i].properties, color:lotesGeoJSON.features[i].properties.color_mod||'#333'}}; }}
                        }} catch(e) {{}}
                    }}
                    return {{ props:nearest?nearest.props:null, color:nearest?nearest.color:'#333', metros:Math.round(minDist) }};
                }}

                function actualizarHUD(lat, lng) {{
                    var dentro = buscarLote(lat, lng);
                    if (dentro) {{
                        var clave = (dentro.modulo||'')+'|'+(dentro.Turno||'')+'|'+(dentro.Lote||'');
                        if (moduloActual !== clave) {{
                            moduloActual = clave;
                            if (navigator.vibrate) navigator.vibrate([200,100,200]);
                            mostrarToast('Entraste a ' + (dentro.modulo||'') + ' · Turno ' + (dentro.Turno||''));
                        }}
                        hud.innerHTML =
                            '<div class="hud-titulo">Estas en</div>' +
                            '<div class="hud-modulo" style="color:' + (dentro.color_mod||'#FFD600') + ';">' + (dentro.modulo||'') + '</div>' +
                            '<div class="hud-detalle">Turno: ' + (dentro.Turno||'—') + ' &nbsp;|&nbsp; Lote: ' + (dentro.Lote||'—') + '</div>';
                    }} else {{
                        moduloActual = null;
                        var c = loteMasCercano(lat, lng);
                        if (!c.props || c.metros > 5000) {{
                            hud.innerHTML = '<div class="hud-modulo">Fuera de zona</div>';
                        }} else {{
                            hud.innerHTML =
                                '<div class="hud-titulo">Mas cercano</div>' +
                                '<div class="hud-modulo" style="color:' + (c.color||'#FFD600') + ';">' + (c.props.modulo||'') + '</div>' +
                                '<div class="hud-detalle">Turno: ' + (c.props.Turno||'—') + ' &nbsp;|&nbsp; Lote: ' + (c.props.Lote||'—') + '</div>' +
                                '<div class="hud-distancia">↗ ' + c.metros + ' m</div>';
                        }}
                    }}
                }}

                function onPosition(pos) {{
                    var lat=pos.coords.latitude, lng=pos.coords.longitude, acc=pos.coords.accuracy;
                    if (!geoMarker) {{
                        geoMarker = L.marker([lat,lng],{{icon:geoIcon}}).addTo(mapEl);
                        accuracyCircle = L.circle([lat,lng],{{radius:acc,color:'#2979FF',fillColor:'#2979FF',fillOpacity:.10,weight:1}}).addTo(mapEl);
                        mapEl.setView([lat,lng],17);
                    }} else {{
                        geoMarker.setLatLng([lat,lng]);
                        accuracyCircle.setLatLng([lat,lng]);
                        accuracyCircle.setRadius(acc);
                    }}
                    actualizarHUD(lat,lng);
                    if (siguiendo) mapEl.panTo([lat,lng]);
                }}

                /* GPS solo en dispositivos táctiles (móvil/tablet) */
                var _esTactil = window.matchMedia('(pointer: coarse)').matches;
                if (navigator.geolocation && _esTactil) {{
                    navigator.geolocation.watchPosition(onPosition,
                        function() {{ hud.innerHTML='<div class="hud-modulo">Sin senal GPS</div>'; }},
                        {{enableHighAccuracy:true, maximumAge:0, timeout:10000}});
                }}

                var LocCtrl = L.Control.extend({{
                    options: {{position:'topleft'}},
                    onAdd: function() {{
                        var btn=L.DomUtil.create('button','');
                        btn.style.cssText='width:34px;height:34px;background:white;border:2px solid rgba(0,0,0,0.3);border-radius:4px;cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 5px rgba(0,0,0,0.4);';
                        btn.innerHTML='&#128205;';
                        L.DomEvent.on(btn,'click',function(e){{L.DomEvent.stopPropagation(e);siguiendo=true;if(geoMarker)mapEl.setView(geoMarker.getLatLng(),17);}});
                        return btn;
                    }}
                }});
                new LocCtrl().addTo(mapEl);
                mapEl.on('dragstart', function() {{ siguiendo=false; }});
            }}

            /* Mostrar HUD solo en móvil */
            if (window.matchMedia('(pointer: coarse)').matches) {{
                document.getElementById('hud').style.display = 'block';
            }}
            initGeo();

            async function activarWakeLock() {{
                if ('wakeLock' in navigator) try {{ await navigator.wakeLock.request('screen'); }} catch(e) {{}}
            }}
            activarWakeLock();
            document.addEventListener('visibilitychange', function() {{
                if (document.visibilityState==='visible') activarWakeLock();
            }});
        }})();
        </script>
        """

        mapa.get_root().html.add_child(folium.Element(js_code))

        mapa_html = mapa._repr_html_()
        responsive_wrapper = f"""<!DOCTYPE html>
<html style="margin:0;padding:0;height:100%;overflow:hidden;">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  *,*::before,*::after{{box-sizing:border-box;}}
  html,body{{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}}
  .folium-map,[id^="map_"]{{width:100%!important;height:100vh!important;min-height:400px;}}
</style>
</head>
<body>
{mapa_html}
<script>
  function _resizeLeaflet(){{
    document.querySelectorAll('.folium-map,[id^="map_"]').forEach(function(d){{d.style.width='100%';d.style.height=window.innerHeight+'px';}});
    var m=Object.values(window).find(function(v){{return v&&v._leaflet_id&&v.invalidateSize;}});
    if(m)m.invalidateSize();
  }}
  window.addEventListener('resize',_resizeLeaflet);
  window.addEventListener('load',_resizeLeaflet);
  [300,800,1500].forEach(function(t){{setTimeout(_resizeLeaflet,t);}});
</script>
</body>
</html>"""

        components.html(responsive_wrapper, height=700, scrolling=False)

    else:
        st.error('No se pudo procesar el archivo KMZ.')