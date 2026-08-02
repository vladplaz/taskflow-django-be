from django.contrib import admin

from .models import Board, BoardInvitation, BoardLabel, BoardMembership, BoardStatus


class BoardStatusInline(admin.TabularInline):
    model = BoardStatus
    extra = 0


class BoardLabelInline(admin.TabularInline):
    model = BoardLabel
    extra = 0


@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "visibility", "is_archived", "updated_at")
    list_filter = ("visibility", "is_archived")
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace", "created_by")
    date_hierarchy = "created_at"
    inlines = [BoardStatusInline, BoardLabelInline]


@admin.register(BoardMembership)
class BoardMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "board", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "user__display_name", "board__name")
    raw_id_fields = ("board", "user")


@admin.register(BoardInvitation)
class BoardInvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "board", "role", "status", "sent_at", "expires_at")
    list_filter = ("status", "role")
    search_fields = ("email", "board__name")
    raw_id_fields = ("board", "invited_user", "invited_by")
    readonly_fields = ("token",)
