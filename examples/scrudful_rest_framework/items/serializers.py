from rest_framework import serializers

from items.models import Item


class ItemSerializer(serializers.ModelSerializer):
    # A ``url`` field makes DRF's CreateModelMixin send a Location header.
    url = serializers.HyperlinkedIdentityField(view_name="item-detail")

    class Meta:
        model = Item
        fields = [
            "id",
            "url",
            "name",
            "description",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["version", "created_at", "updated_at"]
