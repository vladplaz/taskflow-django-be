from django.contrib import admin

from .models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "task", "placement", "content_type", "size", "claimed_at")
    list_filter = ("placement", "content_type")
    search_fields = ("original_name", "task__title")
    raw_id_fields = ("task", "comment", "uploaded_by")
    readonly_fields = ("file", "thumbnail", "size", "content_type", "created_at")
    date_hierarchy = "created_at"
