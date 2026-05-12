"""Phase 1: Batch enrichment of parsed CSD forms from Chorus v1 API."""
import asyncio
import logging
from chorus_forms.csd.models import CsdForm, DictionaryInfo
from chorus_v1_client import ChorusV1Client

logger = logging.getLogger(__name__)


def collect_unique_codes(forms: list[CsdForm]) -> list[str]:
    """Collect deduplicated field codes across all forms."""
    codes = set()
    for form in forms:
        for field in form.fields:
            if field.code:
                codes.add(field.code)
    return sorted(codes)


async def enrich_forms(
    forms: list[CsdForm],
    chorus_client: ChorusV1Client,
    existing_field_cache: dict[str, dict] | None = None,
    existing_domain_cache: dict[str, list] | None = None,
) -> tuple[list[CsdForm], dict[str, dict], dict[str, list]]:
    """Enrich forms with field metadata from the Chorus v1 API.

    Pre-loaded caches are preserved; only missing codes are fetched.
    Returns: (enriched_forms, field_cache, domain_cache)
    """
    field_cache: dict[str, dict] = dict(existing_field_cache or {})
    domain_cache: dict[str, list] = dict(existing_domain_cache or {})

    if not chorus_client.available:
        return forms, field_cache, domain_cache

    codes = collect_unique_codes(forms)
    # Only fetch codes not already in cache
    missing_codes = [c for c in codes if c not in field_cache]
    logger.info("Enriching %d unique field codes (%d cached, %d to fetch)",
                len(codes), len(codes) - len(missing_codes), len(missing_codes))
    if missing_codes:
        new_fields = await chorus_client.get_fields_batch(missing_codes)
        field_cache.update(new_fields)
        logger.info("Fetched %d new field definitions", len(new_fields))

    combo_codes = set()
    for form in forms:
        for field in form.fields:
            if field.control_type in ("combobox", "listbox") and field.code:
                combo_codes.add(field.code)

    # Fetch domain values in parallel (matching get_fields_batch pattern)
    missing_combo = sorted(c for c in combo_codes if c not in domain_cache)
    if missing_combo:
        semaphore = asyncio.Semaphore(10)

        async def _fetch_domain(code: str) -> tuple[str, list | None]:
            async with semaphore:
                return code, await chorus_client.get_domain_values(code)

        domain_results = await asyncio.gather(*[_fetch_domain(c) for c in missing_combo])
        for code, values in domain_results:
            if values:
                domain_cache[code] = [
                    v if isinstance(v, dict) else {"code": v, "expand": v}
                    for v in values
                ]

    logger.info("Fetched domain values for %d fields (%d new)",
                len(domain_cache), len(missing_combo) if missing_combo else 0)

    for form in forms:
        for field in form.fields:
            if field.code in field_cache:
                info = field_cache[field.code].copy()
                if field.code in domain_cache:
                    info["domainValues"] = domain_cache[field.code]
                field.dictionary = DictionaryInfo(**info)

    return forms, field_cache, domain_cache
