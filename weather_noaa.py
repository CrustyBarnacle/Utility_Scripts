#!/usr/bin/env python3
"""Fetch weather forecast from NOAA's public API."""

import requests
import sys

def get_weather(lat: float, lon: float) -> None:
    """Get weather forecast for given coordinates."""
    base_url = "https://api.weather.gov"
    headers = {"User-Agent": "(weather-app, contact@example.com)"}

    # Step 1: Get the forecast URL for these coordinates
    points_url = f"{base_url}/points/{lat},{lon}"
    response = requests.get(points_url, headers=headers)

    if response.status_code != 200:
        print(f"Error getting location data: {response.status_code}")
        sys.exit(1)

    points_data = response.json()
    forecast_url = points_data["properties"]["forecast"]
    location = points_data["properties"]["relativeLocation"]["properties"]
    print(f"Weather for {location['city']}, {location['state']}\n")

    # Step 2: Get the actual forecast
    response = requests.get(forecast_url, headers=headers)

    if response.status_code != 200:
        print(f"Error getting forecast: {response.status_code}")
        sys.exit(1)

    forecast_data = response.json()
    periods = forecast_data["properties"]["periods"]

    # Display first few forecast periods
    for period in periods[:4]:
        print(f"{period['name']}:")
        print(f"  {period['temperature']}°{period['temperatureUnit']}")
        print(f"  {period['shortForecast']}\n")

if __name__ == "__main__":
    # Example: San Francisco coordinates
    get_weather(37.7749, -122.4194)
