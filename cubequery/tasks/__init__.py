import json
import logging
import os
import zipfile
from enum import EnumMeta
from os import path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from flask import url_for

import datacube
from jobtastic import JobtasticTask
from shapely import wkt

from cubequery import get_config
from cubequery.utils.s3_tools import S3Utils

_http_headers = {"Content-Type": "application/json", "User-Agent": "cubequery-result"}


class DType(EnumMeta):
    STRING = "str"
    INT = "int"
    FLOAT = "float"
    LAT = "lat"
    LON = "lon"
    DATE = "date"
    TIME = "time"
    WKT = "wkt"
    MULTI = "multi"


class Parameter(object):
    def __init__(self, name, display_name, d_type, description, valid=None):
        if valid is None:
            valid = []
        self.name = name
        self.display_name = display_name
        self.d_type = d_type
        self.description = description
        self.valid = valid


class CubeQueryTask(JobtasticTask):

    @classmethod
    def cal_significant_kwargs(cls, parameters):
        cls.significant_kwargs = [("params", str)]
        return cls.significant_kwargs

    @classmethod
    def map_d_type_to_jobtastic(cls, d_type):
        # TODO: add more data types here.
        # special handling for dates, lat lon pairs, bounding boxes, etc.
        if d_type == DType.INT:
            return str
        if d_type in (DType.FLOAT, DType.LAT, DType.LON):
            return str
        return str

    def map_kwargs(self, **kwargs):
        result = {}
        logging.info("decoding args")
        for k, v in json.loads(kwargs['params']).items():
            logging.info(f"decoding {k} with value {v}")
            args = [p for p in self.parameters if p.name == k]
            if len(args) > 0:
                arg = args[0]
                logging.info(f"found arg for {k} with type {arg.d_type}")
                if arg.d_type == DType.INT:
                    result[k] = int(v)
                elif arg.d_type in (DType.FLOAT, DType.LAT, DType.LON):
                    result[k] = float(v)
                elif arg.d_type == DType.MULTI:
                    result[k] = v
                else:
                    result[k] = v
            else:
                logging.warning(f"Not found a parameter entry for {k}")
                result[k] = v
        return result

    def validate_arg(self, name, value):
        search = [p for p in self.parameters if p.name == name]
        if len(search) == 0:
            return False, f"parameter {name} not found"

        param = search[0]
        if not validate_d_type(param, value):
            return False, f"parameter {name} value did not validate"
        return True, ""

    def validate_args(self, args):
        """
        Validates conditions based upon the combination of the parameters provided.

        Loads conditions set in input_conditions.json
        
        """
        
        #args = {'user': 'test_user', 'aoi': 'POLYGON((176.77849394078876 -17.133616242815744,179.34929472203808 -17.133616242815744,179.34929472203808 -19.2729492234721,176.77849394078876 -19.2729492234721,176.77849394078876 -17.133616242815744))', 'output_projection': 'EPSG:3460', 'start_date': '1900-03-01', 'end_date': '1902-03-08', 'platform': 'SENTINEL_2', 'res': '7090', 'bands': ['Coastal Aerosol', 'Blue', 'Green', 'Red', 'Near Infrared', 'SWIR 1', 'SWIR 2', 'Panchromatic', 'Cirrus', 'Thermal Infrared 1', 'Thermal Infrared 2'], 'aoi_crs': 'EPSG:4326', 'mosaic_type': 'min', 'indices': 'EVI'}
        #_settings_json = url_for('fetch_form_settings')
        
        _settings_json = None
        
        if not _settings_json:          
            with open('input_conditions.json') as res_json: 
                _settings_json = json.load(res_json)  

        keys = [k for k in _settings_json if k in args]

        errors = []
        
        # Validates AOI
        if 'aoi' in args:
            errors = validate_spatial_query(args['aoi'])
        
        # Validates information against input_conditions.json
        for key in keys:
            for d in _settings_json[key]:
                if d['name'] == args[key]:
                    for condition in d['conditions']:

                        # Integer Range Validation
                        if condition['type'] == 'int_range':
                            for c in condition['id']:
                                if c in args:                            
                                    if len(condition['value'])==2:
                                        if not (int(args[c]) >= condition['value'][0]) or not(int(args[c]) <= condition['value'][1]):
                                            errors.append(create_error_message(condition))
                                    else:
                                        if not (int(args[c]) >= condition['value'][0]):
                                            errors.append(create_error_message(condition))

                        # Date Range Validation
                        if condition['type'] == 'date_range':
                            for c in condition['id']:
                                if c in args:
                                    if len(condition['value'])==2:
                                        if not (args[c] >= condition['value'][0]) or not(args[c] <= condition['value'][1]):
                                            errors.append(create_error_message(condition))
                                    else:
                                        if not (args[c] >= condition['value'][0]):
                                            errors.append(create_error_message(condition))
        
        return errors
    

    def calculate_result(self, **kwargs):
        """
        This is the entry point for a task run. Will be called by celery.

        :param kwargs: arguments to the tasks.
        :return:
        """

        # connect to the datacube and pass that in to the users function.
        # Everything should be talking to the datacube here so makes sense to pull it out and make things
        # easier for the users.
        result_dir = get_config("App", "result_dir")
        path_prefix = path.join(result_dir, self.request.id)

        os.makedirs(path_prefix, exist_ok=True)

        args = self.map_kwargs(**kwargs)

        dc = datacube.Datacube(app=self.name)
        outputs = self.generate_product(dc, path_prefix, **args)
        logging.info(f"got result of {outputs}")
        self.log_query(path_prefix)
        self.zip_outputs(path_prefix, outputs)
        # TODO: put the results some where, send notifications etc.
        output_url = self.upload_results(path_prefix)

        self.ping_results(output_url, args)

    def log_query(self, path_prefix):
        output = path.join(path_prefix, "query.json")
        with open(output, 'w') as f:
            json.dump(self.request.__dict__, f, skipkeys=True)

    def zip_outputs(self, path_prefix, results):
        output = os.path.join(path_prefix, self.request.id + "_output.zip")
        with zipfile.ZipFile(output, 'w') as zf:
            zf.write(path.join(path_prefix, "query.json"), arcname="query.json")
            for f in results:
                zf.write(f, arcname=path.basename(f))

    def upload_results(self, path_prefix):
        source_file_path = os.path.join(path_prefix, self.request.id + "_output.zip")
        dest_file_path = os.path.join(get_config("AWS", "path_prefix"), self.request.id + "_output.zip")

        access_key = get_config("AWS", "access_key_id")
        secret_key = get_config("AWS", "secret_access_key")
        bucket = get_config("AWS", "bucket")

        s3_tools = S3Utils(access_key, secret_key, bucket, get_config("AWS", "s3_endpoint"), get_config("AWS", "region"))

        s3_tools.put_file(source_file_path, dest_file_path)

        return dest_file_path

    def ping_results(self, output_url, results):
        result_url = get_config("App", "result_url")
        if result_url:

            # step one get log in token
            # token = login_to_publisher()

            # step two send payload
            url = f"{get_config('App', 'result_url')}/submit"
            payload = {
                "url": f"http://{get_config('AWS', 's3_endpoint')}/{get_config('AWS', 'bucket')}/{output_url}",
                "name": results['user']
            }
            print(f"payload: {payload}")

            req = Request(url, json.dumps(payload).encode(), headers=_http_headers)
            try:
                resp = urlopen(req)
                if resp == "ok":
                    logging.info("request completed")
            except HTTPError as e:
                logging.error(f"could not log into publish server {e}")
                raise e

    herd_avoidance_timeout = 60
    cache_duration = 60 * 60 * 24  # One day of seconds


def login_to_publisher():

    url = f"{get_config('App', 'result_url')}/token"
    login_payload = {
        'name': get_config("App", "result_login_user"),
        'pass': get_config("App", "result_login_pass")
    }
    req = Request(url, json.dumps(login_payload).encode(), headers=_http_headers)
    try:
        resp = urlopen(req)
        return json.load(resp)['token']
    except HTTPError as e:
        logging.error(f"could not log into publish server {e}")
        raise e

def create_error_message(condition):
    return {'Key':condition['id'], 'Error':condition['error_message'], 'Comment':condition['_comment']}

# TODO: Bounds conversion and sometimes spatial query dependent on product
def validate_spatial_query(value):

    errors = []

    try:
        parsed_polygon = wkt.loads(value)
    except:
        return [create_error_message({'id':'aoi', 'error_message':'Polygon could not be loaded', '_comment':'Polygon could not be loaded'})]

    
    '''
    Returns validity of geometery (bool)
    * Whole of Fiji = True
    * Suva = True
    '''
    valid_geom = parsed_polygon.is_valid
    if not valid_geom:
        errors.append(create_error_message({'id':'aoi', 'error_message':'Geometry not a valid polygon', '_comment':'Geometry not a valid polygon'}))

    '''
    Returns area of polygon - About 1/4 of country ... 0.25 
    * Whole of Fiji = 1.8662849915034905
    * Suva = 0.017204474747948426
    '''
    area = parsed_polygon.area
    if area > 0.25:
        errors.append(create_error_message({'id':'aoi', 'error_message':'AOI area is too large', '_comment':'Size of polygon is too large'}))

    '''
    Returns bool for polygon inside Fiji
    '''
    fiji_polygon = wkt.loads('POLYGON((177.0421658157887 -17.359201951740324,178.9208279251632 -17.359201951740324,178.9208279251632 -18.352613689908015,177.0421658157887 -18.352613689908015,177.0421658157887 -17.359201951740324))')
    contains = fiji_polygon.contains(parsed_polygon)
    if contains == False:
        errors.append(create_error_message({'id':'aoi', 'error_message':'AOI out of Fiji bounds', '_comment':'AOI is either completely or partially out of the Fiji bounds'}))

    return errors


def validate_d_type(param, value):
    if param.d_type == DType.INT:
        return check_int(value)
    if param.d_type == DType.FLOAT:
        return check_float(value)
    if param.d_type == DType.MULTI:
        return True
    if param.d_type == DType.LAT:
        if check_float(value):
            v = float(value)
            return -90.0 <= v <= 90.0
        return False
    if param.d_type == DType.LON:
        if check_float(value):
            v = float(value)
            return -180.0 <= v <= 180.0
        return False
    if param.d_type == DType.WKT:
        # try and parse it and see what happens
        try:
            wkt.loads(value)
            return True
        except Exception:
            return False
    # if it is not one of the above types we can just check it is a string for now.
    # TODO: More type validations. WKT, DateFormats etc.
    return isinstance(value, str)

def check_multi(s):
    if isinstance(s, list):
        return True
    return False

def check_int(s):
    if isinstance(s, int):
        return True
    if isinstance(s, str):
        if len(s) == 0:
            return False
        if s[0] in ('-', '+'):
            return s[1:].isdigit()

        return s.isdigit()
    return False


def check_float(s):
    if isinstance(s, float):
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False
