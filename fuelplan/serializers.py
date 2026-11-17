from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    # Optional overrides of the planning rules; settings.FUEL_PLAN supplies the defaults.
    stop_cost = serializers.FloatField(required=False, min_value=0, max_value=50)
    reserve_miles = serializers.FloatField(required=False, min_value=0, max_value=200)
    min_purchase_gallons = serializers.FloatField(required=False, min_value=0, max_value=40)
