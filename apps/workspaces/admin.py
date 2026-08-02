from django.contrib import admin

from .models import Workspace, WorkspaceInvitation, WorkspaceMembership


class WorkspaceMembershipInline(admin.TabularInline):
    model = WorkspaceMembership
    extra = 0
    raw_id_fields = ("user",)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at")
    search_fields = ("name", "created_by__email")
    raw_id_fields = ("created_by",)
    date_hierarchy = "created_at"
    inlines = [WorkspaceMembershipInline]


@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "user__display_name", "workspace__name")
    raw_id_fields = ("workspace", "user")


@admin.register(WorkspaceInvitation)
class WorkspaceInvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "workspace", "status", "sent_at", "expires_at")
    list_filter = ("status",)
    search_fields = ("email", "workspace__name")
    raw_id_fields = ("workspace", "invited_user", "invited_by")
    readonly_fields = ("token",)
