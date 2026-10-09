"""Reserved test domains; never point these fixtures at an actual learning site."""
from scripts import common as settings

SITE_URL = 'https://learning.example.test'
API_URL = 'https://api.learning.example.test'
CDN_URL = 'https://cdn.learning.example.test'
CATALOG_URL = SITE_URL + '/kng/#/list?catalogId=example'

settings.SITE_ORIGIN = SITE_URL
settings.CATALOG_URL = CATALOG_URL
settings.COURSE_URL = ''
settings.API_HOST_SUFFIXES = ('example.test',)
settings.FOLDER_SUFFIX = '26-10'
