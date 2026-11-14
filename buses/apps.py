from django.apps import AppConfig
import uuid

from django.db import models


class BusesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "buses"
