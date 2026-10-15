# Standard library imports
import os
import gc
import sys
import time
import zipfile
import argparse
import logging
import itertools
from datetime import datetime, timedelta
from functools import partial
from typing import List, Optional
from multiprocessing import cpu_count
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Third-party library imports
import boto3
import pygrib
import swifter
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from botocore import UNSIGNED
from botocore.client import Config
from pydantic import BaseModel, Field

# Function to create directories if they don't exist
def mkdir_p(check_dir):
    Path(check_dir).mkdir(parents=True, exist_ok=True)
    return check_dir

##########################
# Command Line Arguments #
##########################

parser = argparse.ArgumentParser(
    description="Build a PoWT archive pairing Synoptic observations with NBM gridded forecasts.",
    epilog="Example: python powt_archive_generator.py 2024-10-01 2024-10-10 48 6")
parser.add_argument("start_date", help="Start date, YYYY-MM-DD (inclusive)")
parser.add_argument("end_date", help="End date, YYYY-MM-DD (inclusive)")
parser.add_argument("lead_time_hours", type=int, help="NBM forecast hour, e.g. 48")
parser.add_argument("forecast_frequency", type=int, help="Hours between NBM init times, e.g. 1 or 3")
parser.add_argument("--output-dir", default=os.environ.get("POWT_OUTPUT_DIR", "./data/powt"),
                    help="Root directory for obs, GRIB, and archive output "
                         "(default: $POWT_OUTPUT_DIR or ./data/powt)")
parser.add_argument("--log-dir", default=os.environ.get("POWT_LOG_DIR", "./logs"),
                    help="Directory for the debug log (default: $POWT_LOG_DIR or ./logs)")
args = parser.parse_args()

start_date = args.start_date
end_date = args.end_date
fhr = args.lead_time_hours
fhr_freq = args.forecast_frequency

# Configure logging
logging.basicConfig(
    filename=os.path.join(mkdir_p(args.log_dir), 'archiver_debugging.log'),
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

#######################
# User Configurations #
#######################

# Synoptic API token (https://customer.synopticdata.com/)
user_token = os.environ.get("SYNOPTIC_TOKEN")
if not user_token:
    sys.exit("Error: set the SYNOPTIC_TOKEN environment variable to your Synoptic API token.")

# Output directories
output_dir = mkdir_p(args.output_dir)

# Region selection
region_selection = "WR"  # Options: "WR", "SR", "CR", "ER", "CONUS", "CWA", "RFC"

# If CWA/RFC selected, specify the region (e.g., "SLC" for Salt Lake City, "CBRFC" for Colorado Basin)
cwa_selection = None

# Network selection
# Options: "NWS+RAWS", "NWS+RAWS+HADS", "NWS", "RAWS", "HADS", "SNOTEL", "ALL"
network_selection = "NWS"

# Element
element = "powt"

# AWS settings
aws_bucket_nbm = 'noaa-nbm-grib2-pds'
nbm_set = 'core'
nbm_area = 'co'

# Query variables
query_vars = ['TMP', 'PTYPE', 'SNOWLVL', 'RH']  # Example: 'APTMP'

####################
# Global variables #
####################

# Backend APIs
metadata_api = "https://api.synopticdata.com/v2/stations/metadata?"

# Data Query APIs
timeseries_api = "https://api.synopticdata.com/v2/stations/timeseries?"
statistics_api = "https://api.synopticlabs.org/v2/stations/statistics?"
precipitation_api = "https://api.synopticdata.com/v2/stations/precipitation?"

# Assign API to element name
synoptic_apis = {
    'qpf': precipitation_api,
    'maxt': statistics_api,
    'mint': statistics_api,
    'powt': timeseries_api
}

# Synoptic networks mapping
synoptic_networks = {
    "NWS+RAWS+HADS": "1,2,106",
    "NWS+RAWS": "1,2",
    "NWS": "1",
    "RAWS": "2",
    "HADS": "106",
    "SNOTEL": "25",
    "ALL": None
}

# Assign synoptic variable to element name
synoptic_vars = {
    'powt': 'weather_condition,weather_summary,weather_cond_code_synop,' 
            'past_weather_code,weather_cond_code,air_temp,relative_humidity'
}

# Assign stat type to element name
stat_type = {
    'powt': None
}

# Observation hours mapping
ob_hours = {
    'powt': ['0000']
}

# Define mappings for precipitation type (Code Table 4.201)
ptype_lookup = {
    1: "Rain",
    3: "Freezing rain",
    5: "Snow",
    6: "Snow",
    7: "Snow",
    8: "Ice pellets",
}

# Define general variable lookups
variable_lookup = {
    "Apparent temperature": "Apparent Temperature",
    "Temperature": "Surface Temperature",
    "2 metre temperature": "2 Meter Temperature",
    "236": "Snow Level",  # Map 236 to "Snow Level"
    "2 metre relative humidity": "Relative Humidity"
}

# Define column renaming mappings
column_rename_mapping = {
    'Precipitation Type (Snow) (Surface)': 'PSN',
    'Precipitation Type (Rain) (Surface)': 'PRA',
    'Precipitation Type (Ice pellets) (Surface)': 'PPL',
    'Precipitation Type (Freezing rain) (Surface)': 'PZR',
    'Snow Level (0 m above sea level)': 'snowlvl',
    'Surface Temperature (Surface)': 'tsfc',
    '2 Meter Temperature (2 m)': 't2m',
    'Apparent Temperature (2 m)': 'tapp',
    'Relative Humidity (2 m)': 'FXRH'
}

# Convert user input to datetime objects
start_date, end_date = [
    datetime.strptime(date + ' 0000', '%Y-%m-%d %H%M')
    for date in [start_date, end_date]
]

# Build synoptic argument dictionary
synoptic_api_args = {
    'ob_stat': stat_type[element],
    'api': synoptic_apis[element],
    'element': element,
    'interval': 24,  # 24h to poll daily powt data
    'region': region_selection,
    'network_query': synoptic_networks[network_selection],  # Add config feature later
    'vars_query': None if element == 'qpf' else f'{synoptic_vars[element]}',
}

################################
# Define Functions and Methods #
################################

def cwa_list(input_region):

    input_region = input_region.upper()

    region_dict ={
        "WR":["BYZ", "BOI", "LKN", "EKA", "FGZ", "GGW", "TFX", "VEF", "LOX", "MFR",
            "MSO", "PDT", "PSR", "PIH", "PQR", "REV", "STO", "SLC", "SGX", "MTR",
            "HNX", "SEW", "OTX", "TWC"],

        "CR":["ABR", "BIS", "CYS", "LOT", "DVN", "BOU", "DMX", "DTX", "DDC", "DLH",
            "FGF", "GLD", "GJT", "GRR", "GRB", "GID", "IND", "JKL", "EAX", "ARX",
            "ILX", "LMK", "MQT", "MKX", "MPX", "LBF", "APX", "IWX", "OAX", "PAH",
            "PUB", "UNR", "RIW", "FSD", "SGF", "LSX", "TOP", "ICT"],

        "ER":["ALY", "LWX", "BGM", "BOX", "BUF", "BTV", "CAR", "CTP", "RLX", "CHS",
            "ILN", "CLE", "CAE", "GSP", "MHX", "OKX", "PHI", "PBZ", "GYX", "RAH",
            "RNK", "AKQ", "ILM"],

        "SR":["ABQ", "AMA", "FFC", "EWX", "BMX", "BRO", "CRP", "EPZ", "FWD", "HGX",
            "HUN", "JAN", "JAX", "KEY", "MRX", "LCH", "LZK", "LUB", "MLB", "MEG",
            "MAF", "MFL", "MOB", "MRX", "OHX", "LIX", "OUN", "SJT", "SHV", "TAE",
            "TBW", "TSA"]}
    
    if input_region == "CONUS":
        return np.hstack([region_dict[region] for region in region_dict.keys()])
    else:
        return region_dict[input_region]
    
def cwa_list_rfc(input_rfc):

    metadata_api = 'https://api.synopticdata.com/v2/stations/metadata?'

    network_query = (f"&network={synoptic_networks[network_selection]}"
                    if synoptic_networks[network_selection] is not None else '')

    # Assemble the API query
    api_query = (f"{metadata_api}&token={user_token}" + network_query +
                f"&complete=1&sensorvars=1,obrange=20230118") #hardcoded for NBM4.1+

    # Print the API query to output
    # print(api_query)

    # Get the data from the API
    response = requests.get(api_query)
    metadata = pd.DataFrame(response.json()['STATION'])

    # Remove NaNs and index by network, station ID
    metadata = metadata[metadata['MNET_SHORTNAME'].notna()]
    metadata = metadata.set_index(['MNET_SHORTNAME', 'STID'])

    metadata['LATITUDE'] = metadata['LATITUDE'].astype(float)
    metadata['LONGITUDE'] = metadata['LONGITUDE'].astype(float)
    metadata['ELEVATION'] = metadata['ELEVATION'].astype(float)

    metadata = metadata[metadata['LATITUDE'] >= 31]
    metadata = metadata[metadata['LONGITUDE'] <= -103.00]
    metadata = metadata[metadata['STATUS'] == 'ACTIVE']

    geometry = gpd.points_from_xy(metadata.LONGITUDE, metadata.LATITUDE)
    metadata = gpd.GeoDataFrame(metadata, geometry=geometry)

    req = requests.get(
        'https://www.weather.gov/source/gis/Shapefiles/Misc/rf05mr24.zip',

    allow_redirects=True)
    open('rf05mr24.zip', 'wb').write(req.content)

    with zipfile.ZipFile('rf05mr24.zip', 'r') as zip_ref:
        zip_ref.extractall()

    rfc_shp = gpd.read_file('rf05mr24.shp').set_index('BASIN_ID')

    metadata = metadata[metadata.geometry.within(rfc_shp.geometry.loc[input_rfc])]

    rfc_site_list = metadata.index.get_level_values(1).unique()
    rfc_cwa_list = metadata['CWA'].unique()

    return metadata

def create_init_times(start_date, end_date, frequency):
    """
    Creates a list of datetime objects representing initialization times.

    Args:
        start_date: The start date (inclusive).
        end_date: The end date (inclusive).

    Returns:
        A list of datetime objects.
    """
    init_times = []
    current_date = start_date
    while current_date <= end_date:
        init_times.append(current_date + timedelta(hours=0))  # 00:00
        #init_times.append(current_date + timedelta(hours=12)) # 12:00
        #current_date += timedelta(days=1)
        current_date += timedelta(hours=frequency)  # Increment by the specified frequency

    return init_times

def ll_to_index(lat, lon, datalats, datalons):
    """
    Find the nearest grid point index for the given latitude and longitude.
    """
    dist = np.sqrt((datalats - lat)**2 + (datalons - lon)**2)
    return np.unravel_index(np.argmin(dist), datalats.shape)

def relabel_grib_message(grb):
    """
    Relabel a GRIB message to create a human-readable column name.
    """
    # Try to use grb.name first
    name = grb.name

    if name not in variable_lookup:
        try:
            message_parts = grb.__str__().split(":")
            if len(message_parts) > 1:
                name = message_parts[1].strip()
            else:
                name = "Unknown Variable"
        except Exception:
            name = "Unknown Variable"

    if name in variable_lookup:
        label = variable_lookup[name]
    elif name == "Precipitation type":
        try:
            lower_limit = grb.lowerLimit
            label = f"Precipitation Type ({ptype_lookup.get(lower_limit, 'Unknown')})"
        except AttributeError:
            label = "Precipitation Type (Unknown)"
    else:
        label = f"Unknown Variable ({name})"

    level = grb.level
    level_type = grb.typeOfLevel
    if level_type == "heightAboveGround":
        label += f" ({level} m)"
    elif level_type == "heightAboveSea":
        label += f" ({level} m above sea level)"
    elif level_type == "surface":
        label += " (Surface)"

    return label

def convert_kelvin_to_fahrenheit(df, columns):
    """
    Convert the specified columns from Kelvin to Fahrenheit.
    """
    for column in columns:
        if column in df.columns:
            print(f"Converting column {column} from Kelvin to Fahrenheit.")
            df[column] = (df[column] - 273.15) * 9 / 5 + 32
    return df

def calculate_wet_bulb_temperature(T, RH):
    """
    Calculate the wet bulb temperature using the empirical formula.
    T: Temperature in Fahrenheit
    RH: Relative Humidity in percentage (0-100)
    Returns: Wet bulb temperature in Fahrenheit or None if inputs are invalid
    """
    # Check for None or NaN values
    if T is None or RH is None or np.isnan(T) or np.isnan(RH):
        return None

    # Convert temperature from Fahrenheit to Celsius
    T_celsius = (T - 32) * 5.0 / 9.0

    # Calculate wet bulb temperature in Celsius
    T_w_celsius = (
        T_celsius * np.arctan(0.151977 * np.sqrt(RH + 8.313659)) +
        np.arctan(T_celsius + RH) -
        np.arctan(RH - 1.676331) +
        0.00391838 * (RH ** (3/2)) * np.arctan(0.023101 * RH) -
        4.686035
    )

    # Convert wet bulb temperature back to Fahrenheit
    T_w_fahrenheit = T_w_celsius * 9.0 / 5.0 + 32
    return round(T_w_fahrenheit, 2)

def extract_nbm_index(index, nbm_data):

    y, x = index
    return nbm_data[y, x]

def grib_indexer(df, grib_files):
    """
    Process GRIB files dynamically to extract relevant grid point values for the DataFrame.
    """
    for grib_file in grib_files:
        if os.path.isfile(grib_file):
            print(f"Generating index from GRIB file: {grib_file}")
            nbm = pygrib.open(grib_file)

            # If not yet indexed, create grib_index for matching grid points
            if 'grib_index' not in df.columns:
                # print("Creating grib_index for latitude/longitude points.")
                nbmlats, nbmlons = nbm.message(1).latlons()
                # print(f"GRIB file lat/lon grid shape: {nbmlats.shape}")

                df_indexed = df.reset_index()[['stid', 'latitude', 'longitude']].drop_duplicates()

                ll_to_index_mapped = partial(ll_to_index, datalats=nbmlats, datalons=nbmlons)

                df_indexed['grib_index'] = df_indexed.swifter.apply(
                    lambda x: ll_to_index_mapped(x.latitude, x.longitude), axis=1)

                # Extract the grid lat/lon for validation
                extract_nbm_lats_mapped = partial(extract_nbm_index, nbm_data=nbmlats)
                extract_nbm_lons_mapped = partial(extract_nbm_index, nbm_data=nbmlons)

                df_indexed['grib_lat'] = df_indexed['grib_index'].apply(extract_nbm_lats_mapped)
                df_indexed['grib_lon'] = df_indexed['grib_index'].apply(extract_nbm_lons_mapped)

                df_indexed.set_index('stid', inplace=True)

                return df_indexed
        else:
            print(f"GRIB file not found: {grib_file}")

    return df

def process_grib_files(df, grib_file):
    logging.info("Entered process_grib_files function.")
    logging.debug(f"Input DataFrame columns: {df.columns}")
    logging.debug(f"Input DataFrame index: {df.index}")
    logging.debug(f"GRIB file to process: {grib_file}")

    try:
        with pygrib.open(grib_file) as nbm:
            logging.info(f"Successfully opened GRIB file: {grib_file}")
            
            for idx, msg in enumerate(nbm):
                logging.debug(f"Processing GRIB message #{idx}. Message details: {msg}")

                try:
                    name = relabel_grib_message(msg)
                    logging.debug(f"GRIB message relabeled to: {name}")
                except Exception as e:
                    logging.error(f"Error relabeling GRIB message #{idx}: {e}")
                    raise

                if name not in df.columns:
                    logging.debug(f"Column '{name}' not in DataFrame. Adding column with NaN values.")
                    df[name] = np.nan

                extract_nbm_index_mapped = partial(extract_nbm_index, nbm_data=msg.values)
                logging.debug(f"Partial function created for extract_nbm_index with GRIB message values.")

                valid_date = msg.validDate
                logging.debug(f"Valid date of GRIB message: {valid_date}")

                if valid_date in df.index.get_level_values('timestamp'):
                    logging.debug(f"Valid date {valid_date} found in DataFrame index.")
                    try:
                        df.loc[valid_date, name] = df.loc[valid_date]['grib_index'].apply(
                            extract_nbm_index_mapped
                        ).values
                        logging.debug(f"Updated DataFrame at valid date {valid_date} for column {name}.")
                    except Exception as e:
                        logging.error(f"Error applying extract_nbm_index_mapped for valid date {valid_date}: {e}")
                        raise
                else:
                    logging.warning(f"Valid date {valid_date} not found in DataFrame index.")

    except Exception as e:
        logging.critical(f"Critical error encountered when processing GRIB file {grib_file}: {e}")
        raise
    finally:
        gc.collect()
        logging.info("Garbage collection invoked.")

    logging.info("Exiting process_grib_files function.")
    return (df, grib_file)

def fetch_obs_from_API(valid_datetime, cwa='', output_type='json',
                       use_saved=True, **req):

    start_adjusted = (datetime.strptime(valid_datetime, '%Y%m%d%H%M')
                        - timedelta(hours=req["interval"]))

    end_adjusted = datetime.strptime(valid_datetime, '%Y%m%d%H%M')

    valid = True
    cwa_filename = (region_selection if region_selection != 'CWA'
                    else cwa_selection)

    element_label = req['element'] if req['element'] != 'qpf' else \
                        'qpe' + f'{req["interval"]:02d}'


    output_file = mkdir_p(f'{output_dir}/obs_{output_type}/') +\
        f'obs.{element_label}.{req["ob_stat"]}' +\
        f'.{valid_datetime}.{cwa_filename}.{output_type}'

    if os.path.isfile(output_file) & use_saved:
        print(f'Output file exists for:{valid_datetime}')

        with open(output_file, 'r') as f:
            data = Data.model_validate_json(f.read())

            # Convert to DataFrame
            df = data.to_dataframe()
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)

            return df

    else:
        json_file = mkdir_p(f"{output_dir}/obs_json/") +\
            f'obs.{element_label}.{req["ob_stat"]}' +\
            f'.{valid_datetime}.{cwa_filename}.json'

        api_query_args = {
            'api_token':f'&token={user_token}',
            'station_query':f'&cwa={cwa}',
            'network_query':(f'&network={req["network_query"]}'
                                if req["network_query"] is not None else ''),

            'start_date_query':f'&start={start_adjusted.strftime("%Y%m%d%H%M")}',
            'end_date_query':f'&end={end_adjusted.strftime("%Y%m%d%H%M")}',

            'vars_query':(f'&pmode=intervals&interval={req["interval"]}'
                            if req["element"] == 'qpf'
                            else f'&vars={req["vars_query"]}'),
            'stats_query':f'&type={req["ob_stat"]}',
            'timezone_query':'&obtimezone=utc',
            'api_extras':'&units=temp|f&complete=True'}
                #'&fields=name,status,latitude,longitude,elevation'

        api_query = req['api'] + ''.join(
            [api_query_args[k] for k in api_query_args.keys()])

        print(f'Polling API for: {valid_datetime}\n{api_query.replace(user_token, "<token>")}')

        status_code, response_count = None, 0
        while (status_code != 200) & (response_count <= 10):
            print(f'{valid_datetime}, HTTP:{status_code}, #:{response_count}')

            # Don't sleep first try, sleep increasing amount for each retry
            time.sleep(2*response_count)

            response = requests.get(api_query)
            # response.raise_for_status()

            status_code = response.status_code
            response_count += 1

        try:
            response_dataframe = pd.json_normalize(
                response.json()['STATION'])
        except:
            valid = False
        else:
            with open(json_file, 'wb+') as wfp:
                wfp.write(response.content)

    if valid:
        # Example usage
        # with open('sample.json', 'r') as f:
        data = Data.model_validate_json(response.content)

        # Convert to DataFrame
        df = data.to_dataframe()
        df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)

        return df

    else:
        return None

##################
# Define Classes #
##################

class PeriodOfRecord(BaseModel):
    start: str
    end: str

class Providers(BaseModel):
    name: str
    url: str

class Units(BaseModel):
    position: str
    elevation: str

class SensorVariables(BaseModel):
    air_temp: dict = Field(default_factory=dict)
    relative_humidity: dict = Field(default_factory=dict)
    weather_summary: dict = Field(default_factory=dict)
    weather_condition: dict = Field(default_factory=dict)
    weather_cond_code: dict = Field(default_factory=dict)
    past_weather_code: dict = Field(default_factory=dict)
    weather_cond_code_synop: dict = Field(default_factory=dict)

class Observations(BaseModel):
    date_time: List[str]

    air_temp_set_1: List[Optional[float]] = Field(default_factory=list)
    air_temp_set_1d: List[Optional[float]] = Field(default_factory=list)

    relative_humidity_set_1: List[Optional[float]] = Field(default_factory=list)
    relative_humidity_set_1d: List[Optional[float]] = Field(default_factory=list)

    weather_summary_set_1: List[Optional[str]] = Field(default_factory=list)
    weather_summary_set_1d: List[Optional[str]] = Field(default_factory=list)

    weather_condition_set_1: List[Optional[str]] = Field(default_factory=list)
    weather_condition_set_1d: List[Optional[str]] = Field(default_factory=list)

    weather_cond_code_set_1: List[Optional[float]] = Field(default_factory=list)
    weather_cond_code_set_1d: List[Optional[float]] = Field(default_factory=list)

    weather_cond_code_synop_set_1: List[Optional[object]] = Field(default_factory=list)
    weather_cond_code_synop_set_1d: List[Optional[object]] = Field(default_factory=list)

    past_weather_code_set_1: List[Optional[object]] = Field(default_factory=list)
    past_weather_code_set_1d: List[Optional[object]] = Field(default_factory=list)

    class Config:
        # allow_population_by_field_name = True
        validate_by_name = True
        arbitrary_types_allowed = True
        strict = False

class Station(BaseModel):
    ID: str
    STID: str
    NAME: str
    ELEVATION: float
    LATITUDE: float
    LONGITUDE: float
    STATUS: str
    MNET_ID: str
    STATE: str
    TIMEZONE: str
    ELEV_DEM: object
    # NWSZONE: str
    # NWSFIREZONE: str
    # GACC: str
    SHORTNAME: str
    # SGID: str
    COUNTY: str
    # COUNTRY: str
    # WIMS_ID: str
    CWA: str
    PERIOD_OF_RECORD: PeriodOfRecord
    # PROVIDERS: Providers
    UNITS: Units
    SENSOR_VARIABLES: SensorVariables
    OBSERVATIONS: Observations
    QC_FLAGGED: bool
    RESTRICTED: bool

class Data(BaseModel):
    STATION: List[Station]

    def to_dataframe(self) -> pd.DataFrame:
        records = []

        for station in self.STATION:
            for i, date_time in enumerate(station.OBSERVATIONS.date_time):
                record = {
                    'stid': station.STID,
                    'latitude': station.LATITUDE,
                    'longitude': station.LONGITUDE,
                    'elevation': station.ELEVATION,
                    'state': station.STATE,
                    'cwa': station.CWA,
                    'timestamp':date_time,

                    'air_temp_set_1': station.OBSERVATIONS.air_temp_set_1[i] if len(station.OBSERVATIONS.air_temp_set_1) > i else None,
                    'relative_humidity_set_1': station.OBSERVATIONS.relative_humidity_set_1[i] if len(station.OBSERVATIONS.relative_humidity_set_1) > i else None,
                    'weather_summary_set_1': station.OBSERVATIONS.weather_summary_set_1[i] if len(station.OBSERVATIONS.weather_summary_set_1) > i else None,
                    'weather_condition_set_1': station.OBSERVATIONS.weather_condition_set_1[i] if len(station.OBSERVATIONS.weather_condition_set_1) > i else None,
                    'weather_cond_code_set_1': station.OBSERVATIONS.weather_cond_code_set_1[i] if len(station.OBSERVATIONS.weather_cond_code_set_1) > i else None,
                    'past_weather_code_set_1': station.OBSERVATIONS.past_weather_code_set_1[i] if len(station.OBSERVATIONS.past_weather_code_set_1) > i else None,
                    'weather_cond_code_synop_set_1': station.OBSERVATIONS.weather_cond_code_synop_set_1[i] if len(station.OBSERVATIONS.weather_cond_code_synop_set_1) > i else None,

                    'air_temp_set_1d': station.OBSERVATIONS.air_temp_set_1d[i] if len(station.OBSERVATIONS.air_temp_set_1d) > i else None,
                    'relative_humidity_set_1d': station.OBSERVATIONS.relative_humidity_set_1d[i] if len(station.OBSERVATIONS.relative_humidity_set_1d) > i else None,
                    'weather_summary_set_1d': station.OBSERVATIONS.weather_summary_set_1d[i] if len(station.OBSERVATIONS.weather_summary_set_1d) > i else None,
                    'weather_condition_set_1d': station.OBSERVATIONS.weather_condition_set_1d[i] if len(station.OBSERVATIONS.weather_condition_set_1d) > i else None,
                    'weather_cond_code_set_1d': station.OBSERVATIONS.weather_cond_code_set_1d[i] if len(station.OBSERVATIONS.weather_cond_code_set_1d) > i else None,
                    'past_weather_code_set_1d': station.OBSERVATIONS.past_weather_code_set_1d[i] if len(station.OBSERVATIONS.past_weather_code_set_1d) > i else None,
                    'weather_cond_code_synop_set_1d': station.OBSERVATIONS.weather_cond_code_synop_set_1d[i] if len(station.OBSERVATIONS.weather_cond_code_synop_set_1d) > i else None
                }
                records.append(record)

        df = pd.DataFrame(records)
        return df

class NBMGribFetcher:

    def __init__(self, aws_bucket, element, nbm_set, nbm_area, query_vars, save_dir):
        self.aws_bucket = aws_bucket
        self.element = element
        self.nbm_set = nbm_set
        self.nbm_area = nbm_area
        self.query_vars = query_vars
        self.save_dir = save_dir
        self.produced_files = []

    def get_nbm_grib_aws(self, aws_request):
        yyyymmdd = datetime.strftime(aws_request['init_time'], '%Y%m%d')
        hh = datetime.strftime(aws_request['init_time'], '%H')

        os.makedirs(self.save_dir, exist_ok=True)

        output_file = (self.save_dir +
                       f"/{yyyymmdd}.t{hh}z.fhr{aws_request['fhr']:03d}.{aws_request['element']}.grib2")

        if os.path.isfile(output_file):
            self.produced_files.append(output_file)
            return output_file

        client = boto3.client('s3', config=Config(signature_version=UNSIGNED))

        bucket_dir = f'blend.{yyyymmdd}/{hh}/{self.nbm_set}/'
        grib_file = f'{bucket_dir}blend.t{hh}z.{self.nbm_set}.f{aws_request["fhr"]:03d}.{self.nbm_area}.grib2'
        index_file = f'{grib_file}.idx'

        try:
            print(self.aws_bucket, index_file)

            index_data_raw = client.get_object(
                Bucket=self.aws_bucket, Key=index_file)['Body'].read().decode().split('\n')

        except Exception as e:
            print(f"Error fetching index file: {e}")
            client.close()
            return

        cols = ['num', 'byte', 'date', 'var', 'level', 'forecast', 'fthresh', 'ftype']
        index_data = []
        for item in index_data_raw:
            item_split = item.split(':')
            if len(item_split) >= 8:
                index_data.append(item_split[:8])
            elif len(item_split) == 7:  # Handle cases where columns are missing
                item_split.append('')
                index_data.append(item_split[:8])

        index_data = pd.DataFrame(index_data, columns=cols)

        index_data = index_data[index_data['num'] != '']
        index_data['num'] = index_data['num'].astype(int)
        index_data = index_data.set_index('num')
        index_data.loc[index_data.shape[0] + 1] = [''] * index_data.shape[1]

        index_subset = index_data[index_data['var'].isin(self.query_vars)]
        index_subset = index_subset[~index_subset['fthresh'].str.contains(
            '% level|std dev', na=False)]

        for i in index_subset.index:
            if (int(i) + 1) in index_data.index:
                index_subset.at[i, 'byte'] = (
                    index_data.at[i, 'byte'],
                    index_data.at[int(i) + 1, 'byte'])
            else:
                index_subset.at[i, 'byte'] = (
                    index_data.at[i, 'byte'], '')

        # Fetch only the selected variables
        with open(output_file, 'wb') as wfp:
            for index, item in index_subset.iterrows():
                byte_range = f"bytes={item['byte'][0]}-{item['byte'][1]}"
                output_bytes = client.get_object(
                    Bucket=self.aws_bucket, Key=grib_file, Range=byte_range)
                for chunk in output_bytes['Body'].iter_chunks(chunk_size=4096):
                    wfp.write(chunk)

        client.close()
        self.produced_files.append(output_file)
        return output_file

    def fetch_for_init_times(self, init_times, fhr):
        with ThreadPoolExecutor(max_workers=cpu_count()) as executor:
            futures = [executor.submit(self.get_nbm_grib_aws, {
                'init_time': init_time,
                'fhr': fhr,
                'element': self.element
            }) for init_time in init_times]

            for future in as_completed(futures):
                result = future.result()
                print(f"Downloaded: {result}")

        return self.produced_files
    
########
# Main #
########

if __name__ == "__main__":

    # Build an iterable date list from range
    iter_date = start_date
    valid_date_iterable = []
    valid_datetime_iterable = []
    forecast_datetime_iterable = []

    while iter_date <= end_date:

        valid_date_iterable.append(iter_date.strftime('%Y%m%d'))

        for hour_range in ob_hours[element]:
            end_hour = f'{int(hour_range[-1]):02d}'

            valid_datetime_iterable.append(iter_date.strftime('%Y%m%d') + end_hour)

            forecast_datetime_iterable.append(
                    # (iter_date-timedelta(days=lead_days_selection)
                    (iter_date-timedelta(hours=fhr)
                ).strftime('%Y%m%d') + end_hour)

        iter_date += timedelta(days=1)

    if region_selection == 'CWA':
        cwa_query = cwa_selection
    elif region_selection == 'RFC':
        rfc_metadata = cwa_list_rfc(cwa_selection)
        cwa_query = ','.join([str(cwa) for cwa in rfc_metadata['CWA'].unique()
                    if cwa is not None])
    else:
        cwa_query = ','.join(cwa_list(region_selection))

    # determine which selection to use based on the condition
    cwa_reg_sel = cwa_selection.replace(',', '-') if region_selection.lower() == "cwa" else region_selection

    # generate the filename using the globals
    obs_filename = f"{cwa_reg_sel}_{start_date.strftime('%Y%m%d%H')}_{end_date.strftime('%Y%m%d%H')}.powt-obs.csv"
    obs_output_path = mkdir_p(f"{output_dir}/obs/") + obs_filename

    if os.path.isfile(obs_output_path):
        print(f"File {obs_output_path} already exists.")
        obs = pd.read_csv(obs_output_path, index_col=[0, 1], parse_dates=[0])
        
    else:
        multiprocess_function = partial(fetch_obs_from_API,
                                    cwa=cwa_query,
                                    **synoptic_api_args)

        df_list = []
        # Multithreaded requests currently not supported by the Synoptic API
        for iter_item in valid_datetime_iterable:
            df_list.append(multiprocess_function(iter_item))

        print('Data acquisition complete. Beginning concatenation.')

        df = pd.concat(df_list).set_index(['timestamp', 'stid']).sort_index().drop_duplicates()

        print('Data concatenation complete.')

        # Combine similarly named columns
        for col in df.columns:
            if col.endswith('_1'):
                new_col_name = col[:-6]  # Remove the last two characters (_1)
                df[new_col_name] = df[col].combine_first(df[col + 'd'])  # Keep non-NaN values

        # Drop the original _1 and _1d columns if needed
        obs = df.drop(columns=[col for col in df.columns if col.endswith(('_1', '_1d'))])

        # Apply the function to the dataframe
        obs['wet_bulb_temp'] = obs.apply(
            lambda row: calculate_wet_bulb_temperature(row['air_temp'], row['relative_humidity']), axis=1
        )
        # Drop rows with NaN values in the 'wet_bulb_temp' column
        obs.dropna(subset=['wet_bulb_temp'], inplace=True)

        obs.rename(columns={'air_temp':'observed_air_temp', 'wet_bulb_temp':'observed_wet_bulb_temp'}, inplace=True)
        obs['weather_condition'] = obs['weather_condition'].astype(str)

        # Extract unique weather conditions
        raw_wx_conds = [v.split(',') for v in obs['weather_condition'].values if v is not None]
        wx_conds = np.hstack([v.split('/') for v in np.unique(np.hstack(raw_wx_conds))])

        wx_counts = obs['weather_condition'].str.split(',|/').explode().value_counts()
        wx_counts = wx_counts.sort_values(ascending=False)

        # RA: Contains "rain" or "drizzle" but not "frz"
        obs['RA'] = obs['weather_condition'].str.contains(r'\brain\b|\bdrizzle\b', case=False, na=False) & \
                    ~obs['weather_condition'].str.contains(r'\bfrz\b', case=False, na=False)

        # SN: Contains "snow", "grains", or "graupel"
        obs['SN'] = obs['weather_condition'].str.contains(r'\bsnow\b|\bgrains\b|\bgraupel\b', case=False, na=False)

        # ZR: Contains "frz" but not "fog"
        obs['ZR'] = obs['weather_condition'].str.contains(r'\bfrz\b', case=False, na=False) & \
                    ~obs['weather_condition'].str.contains(r'\bfog\b', case=False, na=False)

        # PL: Contains "ice" or "pellets" but not "fog"
        obs['PL'] = obs['weather_condition'].str.contains(r'\bice\b|\bpellets\b', case=False, na=False) & \
                    ~obs['weather_condition'].str.contains(r'\bfog\b', case=False, na=False)

        # UP: Contains "unknown"
        obs['UP'] = obs['weather_condition'].str.contains(r'\bunknown\b', case=False, na=False)& \
                    ~obs['weather_condition'].str.contains(r'\bfog\b', case=False, na=False)

        # Create a dictionary to store combination counts
        combination_counts = {}

        # Iterate through all possible combinations of conditions
        for i in range(1, 5):  # Iterate through combinations of 1 to 4 conditions
            for combo in itertools.combinations(['RA', 'SN', 'ZR', 'PL', 'UP'], i):
                # Create a boolean series indicating if the combination is present
                combo_series = obs[list(combo)].all(axis=1)

                # Count the occurrences of the combination
                combination_counts[','.join(combo)] = combo_series.sum()

        obs.to_csv(obs_output_path)
        print('Saved', obs_output_path)

    obs.reset_index(inplace=True)
    obs['timestamp'] = obs['timestamp'].astype('datetime64[ns]')
    obs['ob_timestamp'] = obs['timestamp'].copy()
    obs['timestamp'] = obs['timestamp'].dt.round('1h')

    obs.set_index(['timestamp', 'stid'], inplace=True)
    obs.sort_index(inplace=True)
    
    columns_order = ["ob_timestamp"] + [col for col in obs.columns if col != "ob_timestamp"]
    obs = obs[columns_order]

    #####################
    # Process NBM Files #
    #####################

    init_times = create_init_times(start_date, end_date, frequency=fhr_freq)

    temp_file = mkdir_p(f"{output_dir}/nbm/tmp/") + f"{cwa_reg_sel}_{start_date.strftime('%Y%m%d%H')}_{end_date.strftime('%Y%m%d%H')}.f{fhr:03d}_freq{fhr_freq:02d}.powt-nbm-obs.TEMP.csv"
    subset_file = mkdir_p(f"{output_dir}/nbm/") + f"{cwa_reg_sel}_{start_date.strftime('%Y%m%d%H')}_{end_date.strftime('%Y%m%d%H')}.f{fhr:03d}_freq{fhr_freq:02d}.powt-nbm-obs.csv"
    
    if os.path.isfile(subset_file):
        subset = pd.read_csv(subset_file, index_col=[0, 1], parse_dates=[0])
        print(f"File {subset_file} already exists.")

    else:
        if os.path.isfile(temp_file):
            processed_df = pd.read_csv(temp_file, index_col=[0, 1], parse_dates=[0])
            print(f"File {temp_file} already exists.")

        else:
            fetcher = NBMGribFetcher(aws_bucket_nbm, element, nbm_set, nbm_area, query_vars, output_dir+'/nbm_grib')
            nbm_files = fetcher.fetch_for_init_times(init_times, fhr)
            
            nbm_files = sorted(nbm_files)
            
            print(f"{len(nbm_files)} NBM GRIB files to process.")

            grib_index = grib_indexer(obs, nbm_files)
            obs = obs.join(grib_index[['grib_index', 'grib_lat', 'grib_lon']], on='stid')

            successful_files = []
            failed_files = []

            for nbm_file in nbm_files[:]:  # Iterate over a copy of the list
                print(f"Processing {nbm_file} (rem:{len(nbm_files)}).")

                try:
                    # Call your function to process the file
                    obs, processed_file = process_grib_files(obs, nbm_file)
                    
                    # Mark the file as successfully processed
                    successful_files.append(nbm_file)
                    
                    # Remove the processed file from the list
                    nbm_files.remove(nbm_file)  # Remove the current file
                    
                    # Optionally log or use the returned data
                    logging.info(f"Processed: {processed_file}")
                    logging.info(f"{len(nbm_files)} files remaining to process.")
            
                except Exception as e:
                    # Handle errors gracefully
                    logging.error(f"Error processing {nbm_file}: {e}")
                    
                    # Mark the file as failed
                    failed_files.append(nbm_file)
                    
                    # Optionally remove the file to avoid retrying it
                    nbm_files.remove(nbm_file)

            # Retry failed files once
            if failed_files:
                logging.info(f"Retrying {len(failed_files)} failed files.")
                for nbm_file in failed_files[:]:  # Iterate over a copy of the failed files list
                    try:
                        # Retry processing the file
                        obs, processed_file = process_grib_files(obs, nbm_file)
                        
                        # Mark the file as successfully processed
                        successful_files.append(nbm_file)
                        
                        # Remove the retried file from the failed list
                        failed_files.remove(nbm_file)
                        
                        logging.info(f"Successfully retried: {processed_file}")
                    except Exception as e:
                        # Log the failure again if it still fails
                        logging.error(f"Retry failed for {nbm_file}: {e}")

            # Log final status
            logging.info(f"Processing complete. {len(successful_files)} files processed successfully.")
            if failed_files:
                logging.warning(f"{len(failed_files)} files failed to process after retry: {failed_files}")

            obs.to_csv(temp_file, index=True)
            print('Saved', temp_file)
            processed_df = obs

        # Rename columns based on the mapping
        processed_df.rename(columns=column_rename_mapping, inplace=True)

        # Convert specified columns from Kelvin to Fahrenheit
        columns_to_convert = ['t2m', 'tsfc', 'tapp']
        processed_df = convert_kelvin_to_fahrenheit(processed_df, columns_to_convert)

        processed_df.rename(columns={'observed_air_temp':'T', 'observed_wet_bulb_temp':'Tw', 'relative_humidity':'RH', 't2m':'FXT'}, inplace=True)
        processed_df['snowlvl'] = processed_df['snowlvl'] * 3.28084 # m to ft

        subset = processed_df[['T', 'FXT', 'RH', 'FXRH', 'Tw', 'elevation', 'snowlvl', 'RA', 'SN', 'ZR', 'PL', 'UP', 'PRA', 'PZR', 'PSN', 'PPL']]

        # Must drop NA before applying the Tw function
        subset = subset.dropna(how='any').copy()

        # Apply the Tw function to the dataframe
        subset = subset.copy()  # Ensure a deep copy to avoid SettingWithCopyWarning
        subset.loc[:, 'FXTw'] = subset.apply(lambda row: calculate_wet_bulb_temperature(row['FXT'], row['FXRH']), axis=1)
        subset = subset[['T', 'FXT', 'RH', 'FXRH', 'Tw', 'FXTw', 'elevation', 'snowlvl', 'RA', 'SN', 'ZR', 'PL', 'UP', 'PRA', 'PZR', 'PSN', 'PPL']]

        subset = subset.apply(lambda col: col.round(2) if np.issubdtype(col.dtype, np.number) else col)

        subset.to_csv(subset_file, index=True)
        print(f"NBM/OBS Subset DataFrame saved to {subset_file}")