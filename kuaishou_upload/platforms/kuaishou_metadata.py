import hashlib


def series_key(job):
    value = f'{job.manifest.seriesTitle}\0{job.manifest.filing.number}\0{job.asset_hashes["copyright"]}'
    return hashlib.sha256(value.encode()).hexdigest()
