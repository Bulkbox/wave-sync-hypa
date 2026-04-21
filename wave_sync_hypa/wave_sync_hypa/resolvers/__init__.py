"""Map Wave identifiers to ERP document names.

Pure lookup and lightweight create-if-missing logic. Resolvers never mutate
existing ERP records beyond the `wave_*` custom fields the app owns.
"""
