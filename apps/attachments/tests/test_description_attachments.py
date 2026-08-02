"""Saving a description carries ids only: it can never create, copy, or
destroy a file."""

from django.core.files.storage import default_storage
from rest_framework import status

from apps.attachments.models import Attachment
from apps.attachments.services import extract_attachment_ids
from apps.tasks.models import Task
from taskflow.testing import create_board, create_board_list, create_task, create_workspace

from . import AttachmentTestCase, image_upload, upload


def document(*nodes) -> dict:
    return {"type": "doc", "content": list(nodes)}


def image_node(attachment_id: int) -> dict:
    return {"type": "attachmentImage", "attrs": {"attachmentId": attachment_id}}


def paragraph_with_link(text: str, attachment_id: int) -> dict:
    """Mirrors the editor's output: a file chip flowing inside a sentence."""
    return {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": text},
            {"type": "attachmentLink", "attrs": {"attachmentId": attachment_id}},
            {"type": "text", "text": ", please check"},
        ],
    }


class ExtractAttachmentIdTests(AttachmentTestCase):
    def test_finds_ids_at_every_depth(self):
        content = document(
            image_node(1),
            paragraph_with_link("i have this file ", 2),
            {"type": "bulletList", "content": [{"type": "listItem", "content": [image_node(3)]}]},
        )

        self.assertEqual(extract_attachment_ids(content), [1, 2, 3])

    def test_ignores_everything_else(self):
        content = document(
            {"type": "paragraph", "content": [{"type": "text", "text": "attachmentId: 7"}]},
            {"type": "image", "attrs": {"attachmentId": 8}},
        )

        self.assertEqual(extract_attachment_ids(content), [])

    def test_reports_each_id_once(self):
        content = document(image_node(4), image_node(4))

        self.assertEqual(extract_attachment_ids(content), [4])

    def test_a_non_integer_id_is_not_a_reference(self):
        content = document({"type": "attachmentImage", "attrs": {"attachmentId": "4 OR 1=1"}})

        self.assertEqual(extract_attachment_ids(content), [])

    def test_deep_nesting_does_not_blow_the_stack(self):
        """The document is attacker-supplied, so the walk is iterative."""
        content = image_node(5)
        for _ in range(5_000):
            content = {"type": "blockquote", "content": [content]}

        self.assertEqual(extract_attachment_ids(document(content)), [5])


class DescriptionReferenceTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.attachment = self.make_attachment(image_upload())

    def patch_description(self, content, task=None):
        return self.client.patch(
            self.task_url(task),
            {"description": content},
            format="json",
        )

    def test_a_document_may_reference_the_task_own_files(self):
        response = self.patch_description(document(image_node(self.attachment.id)))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            extract_attachment_ids(response.data["description"]),
            [self.attachment.id],
        )

    def test_saving_the_same_description_twice_changes_nothing(self):
        """The regression this whole design exists to prevent."""
        content = document(image_node(self.attachment.id))
        self.patch_description(content)

        for _ in range(3):
            self.assertEqual(self.patch_description(content).status_code, status.HTTP_200_OK)

        self.assertEqual(Attachment.objects.filter(task=self.task).count(), 1)
        self.assertTrue(default_storage.exists(self.attachment.file.name))

    def test_dropping_a_reference_keeps_the_file(self):
        """The strip owns it; a backspace in the editor must not destroy it."""
        self.patch_description(document(image_node(self.attachment.id)))

        response = self.patch_description(document({"type": "paragraph"}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Attachment.objects.filter(pk=self.attachment.pk).exists())
        self.assertTrue(default_storage.exists(self.attachment.file.name))
        self.assertEqual(len(response.data["attachments"]), 1)

    def test_a_file_from_another_task_is_refused(self):
        """Otherwise a document could embed, and then read, a private board's file."""
        other = create_task(board_list=self.board_list, creator=self.owner, title="Other")
        foreign = self.make_attachment(task=other)

        response = self.patch_description(document(image_node(foreign.id)))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("do not belong", str(response.data["description"][0]))

    def test_a_file_from_another_workspace_is_refused(self):
        other_workspace = create_workspace(owner=self.owner, name="Theirs")
        other_task = create_task(
            board_list=create_board_list(
                board=create_board(workspace=other_workspace, creator=self.owner)
            ),
            creator=self.owner,
        )
        foreign = Attachment.objects.create(
            task=other_task,
            placement=Attachment.Placement.TASK,
            file="attachments/files/1/x.txt",
            original_name="x.txt",
            content_type="text/plain",
            size=1,
        )

        response = self.patch_description(document(image_node(foreign.id)))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_comment_file_cannot_be_embedded_in_the_description(self):
        comment_file = self.make_attachment(placement=Attachment.Placement.COMMENT)

        response = self.patch_description(document(image_node(comment_file.id)))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_deleted_file_is_dropped_from_the_document(self):
        """Two tabs open, one deletes: the other's save must still go through."""
        stale_id = self.attachment.id
        content = document(
            image_node(stale_id),
            paragraph_with_link("i have this file ", stale_id),
        )
        self.patch_description(content)
        self.attachment.delete()

        response = self.patch_description(content)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(extract_attachment_ids(response.data["description"]), [])
        self.assertEqual(
            extract_attachment_ids(Task.objects.get(pk=self.task.pk).description_content),
            [],
        )
        self.assertFalse(Attachment.objects.filter(pk=stale_id).exists())

    def test_removing_a_stale_reference_leaves_the_surrounding_text(self):
        stale_id = self.attachment.id
        self.attachment.delete()

        response = self.patch_description(
            document(paragraph_with_link("i have this file ", stale_id))
        )

        paragraph = response.data["description"]["content"][0]
        self.assertEqual(
            [node["text"] for node in paragraph["content"]],
            ["i have this file ", ", please check"],
        )

    def test_a_new_task_cannot_reference_anything(self):
        """Nothing has been uploaded to a task that does not exist yet."""
        response = self.client.post(
            f"/api/v1/workspaces/{self.workspace.id}/lists/{self.board_list.id}/tasks/",
            {"title": "New", "description": document(image_node(self.attachment.id))},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_from_the_strip_is_what_removes_the_file(self):
        self.patch_description(document(image_node(self.attachment.id)))
        file_name = self.attachment.file.name

        with self.commit():
            response = self.client.delete(self.detail_url(self.attachment))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(default_storage.exists(file_name))


class TaskRepresentationTests(AttachmentTestCase):
    def test_detail_lists_only_the_strip(self):
        strip_file = self.make_attachment(upload("plan.txt"))
        self.make_attachment(placement=Attachment.Placement.COMMENT)

        response = self.client.get(self.task_url())

        self.assertEqual([item["id"] for item in response.data["attachments"]], [strip_file.id])
        self.assertEqual(response.data["attachment_count"], 1)

    def test_the_listing_counts_without_loading_the_rows(self):
        self.make_attachment(upload("a.txt"))
        self.make_attachment(upload("b.txt"))
        self.make_attachment(placement=Attachment.Placement.COMMENT)

        response = self.client.get(
            f"/api/v1/workspaces/{self.workspace.id}/lists/{self.board_list.id}/tasks/"
        )

        row = response.data["results"][0]
        self.assertEqual(row["attachment_count"], 2)
        self.assertNotIn("attachments", row)

    def test_the_listing_stays_in_position_order(self):
        """Aggregating drops Meta.ordering, so the view restores it."""
        for index in range(3):
            create_task(
                board_list=self.board_list,
                creator=self.owner,
                title=f"Task {index}",
                position=100 - index * 10,
            )

        response = self.client.get(
            f"/api/v1/workspaces/{self.workspace.id}/lists/{self.board_list.id}/tasks/"
        )

        positions = [float(row["position"]) for row in response.data["results"]]
        self.assertEqual(positions, sorted(positions))

    def test_a_freshly_created_task_still_reports_a_count(self):
        """No annotation and no prefetch on this path -- the fallback covers it."""
        response = self.client.post(
            f"/api/v1/workspaces/{self.workspace.id}/lists/{self.board_list.id}/tasks/",
            {"title": "Fresh"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["attachment_count"], 0)

    def test_urls_point_at_the_api_not_at_storage(self):
        """A storage URL would either be public or expire; this one is neither."""
        attachment = self.make_attachment(image_upload())

        response = self.client.get(self.task_url())

        item = response.data["attachments"][0]
        self.assertEqual(item["url"], self.detail_url(attachment))
        self.assertEqual(item["thumbnail_url"], f"{self.detail_url(attachment)}?variant=thumb")
        self.assertTrue(item["can_preview"])
