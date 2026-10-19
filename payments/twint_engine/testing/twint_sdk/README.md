<!-- //// Neoffice — added file (no upstream equivalent): fixtures of the TWINT engine's tests. -->
# From the official TWINT PHP SDK

Copied from github.com/Twint-AG/sdk at `2.0.0` (`1d68501c550511011feac4d5d6dec594356a5287`), MIT per the SDK's composer.json (maintainer: TWINT), by `neoffice-devops/scripts/twint_engine_bench/import_sdk_fixtures.py`.

- `<version>/*.wsdl`, `*.xsd`: the service's schemas, against which the tests validate every request the engine writes and every answer the fake server gives.
- `<version>/stubs.json`: the answers the SDK's hermetic test lane serves through WireMock, trimmed to the operations the engine speaks. The fake server (`../fake_twint.py`) renders them.
