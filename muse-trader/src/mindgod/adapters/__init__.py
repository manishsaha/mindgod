"""Implementations of the application ports: venue clients, odds feeds,
storage, Discord. Venue formats never leak past this layer: adapters
translate every venue market into canonical outcomes and terms with the
builders in domain.propositions.
"""
