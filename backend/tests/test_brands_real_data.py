def test_manual_and_custom_groups(brand_resolver):
    assert brand_resolver.same("MANN-FILTER", "MANN")
    assert brand_resolver.same("Knecht", "MAHLE")
    assert brand_resolver.same("KIA", "HYUNDAI")
    assert brand_resolver.same("Lucas", "TRW")
    assert not brand_resolver.same("BOSCH", "MANN")


def test_title_of_group(brand_resolver):
    assert brand_resolver.title("MANN FILTER") == "MANN"


def test_provider_brand_name_mapping(brand_resolver):
    # Как бренд называется у конкретного поставщика (brand_provider_mappings.json)
    assert brand_resolver.for_provider("MANN", "MikadoProvider") == "MANN-FILTER"
    assert brand_resolver.for_provider("MANN", "PrLgProvider") == "MANN"
    assert brand_resolver.for_provider("MAHLE", "ForumAutoProvider") == "KNECHT"
