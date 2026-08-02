from django.contrib import admin

from .models import BoardList, Task, TaskActivity, TaskAssignment, TaskComment


@admin.register(BoardList)
class BoardListAdmin(admin.ModelAdmin):
    list_display = ("name", "board", "position", "is_archived", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("name", "board__name")
    raw_id_fields = ("board",)


class TaskAssignmentInline(admin.TabularInline):
    model = TaskAssignment
    extra = 0
    raw_id_fields = ("membership",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "board_list", "priority", "status", "is_archived", "updated_at")
    list_filter = ("priority", "is_archived")
    search_fields = ("title", "board_list__name")
    raw_id_fields = ("board_list", "status", "created_by")
    filter_horizontal = ("labels",)
    date_hierarchy = "created_at"
    inlines = [TaskAssignmentInline]


@admin.register(TaskActivity)
class TaskActivityAdmin(admin.ModelAdmin):
    list_display = ("task", "action", "author", "created_at")
    list_filter = ("action",)
    search_fields = ("task__title", "author__email")
    raw_id_fields = ("task", "author")
    date_hierarchy = "created_at"


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ("task", "updated_at")
    search_fields = ("body", "task__title")
    raw_id_fields = ("task", "activity")
    filter_horizontal = ("mentions",)


@admin.register(TaskAssignment)
class TaskAssignmentAdmin(admin.ModelAdmin):
    list_display = ("task", "membership", "assigned_at")
    search_fields = ("task__title", "membership__user__email")
    raw_id_fields = ("task", "membership")
