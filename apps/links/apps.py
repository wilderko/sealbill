from django.apps import AppConfig


class LinksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.links"
    verbose_name = "Payment links"

    def ready(self):
        # Pillow's decompression-bomb limit, brought down to the link image cap for the whole process,
        # once at start-up (final review m2): before, only the first link image set lowered it, so an
        # organization logo or a PDF image read earlier met Pillow's own, larger limit.
        from PIL import Image

        from .services import MAX_IMAGE_PIXELS

        if Image.MAX_IMAGE_PIXELS is None or Image.MAX_IMAGE_PIXELS > MAX_IMAGE_PIXELS:
            Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
