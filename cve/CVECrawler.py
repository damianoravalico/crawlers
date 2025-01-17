import json
import logging
import os
import time
import requests
import datetime
import argparse


class CVECrawler:
    def __init__(self,
                 storage_path='/usr/src/data',
                 request_timeout=60,
                 interval_between_requests=6,  # Suggested by NIST
                 update_interval=7200,  # Suggested by NIST
                 retry_interval=600,
                 retries_for_request=9,
                 mode='info'):
        self.storage_path = storage_path
        self.request_timeout = request_timeout
        self.interval_between_requests = interval_between_requests
        self.update_interval = update_interval
        self.retry_interval = retry_interval
        self.retries_for_request = retries_for_request
        self.mode = mode
        if self.mode not in ['info', 'changes']:
            logging.error(f'{self.mode} is not a valid mode')
            exit(1)
        if self.mode == 'info':
            self.ENDPOINT_NIST = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
        if self.mode == 'changes':
            self.ENDPOINT_NIST = 'https://services.nvd.nist.gov/rest/json/cvehistory/2.0'
        self.INDEX_FILENAME = '.index.txt'
        self.LAST_UPDATE_FILENAME = '.last_timestamp.txt'
        self.MISSING_INDEXES = 'missing_indexes.txt'
        log_format = f'[%(asctime)s] [%(levelname)s] %(message)s'
        logging.basicConfig(level=logging.INFO, format=log_format, datefmt='%Y-%m-%d %H:%M:%S')

    def run(self):
        logging.info('Crawler up')
        os.makedirs(self.storage_path, exist_ok=True)
        logging.info('Initialisation of the data population')
        self.init_data_population()
        logging.info('Initialisation completed')

        with open(os.path.join(self.storage_path, self.LAST_UPDATE_FILENAME), 'w') as file:
            file.write(str(datetime.datetime.now().isoformat()))
        while True:
            logging.info(f'Going to sleep for {self.update_interval} seconds due to normal stand-by mode')
            time.sleep(self.update_interval)
            logging.info('Crawler woke up from stand-by mode')
            logging.info(f'Starting the cycle...')
            self.maintain_data()

    def init_data_population(self):
        try:
            with open(os.path.join(self.storage_path, self.INDEX_FILENAME), 'r') as file:
                index = int(file.read().strip())
        except:
            index = 0
        if self.mode == 'info':
            entries_for_request = 2000
        else:
            entries_for_request = 5000
        actual_retries = 0
        while True:
            with open(os.path.join(self.storage_path, self.INDEX_FILENAME), 'w') as file:
                file.write(str(index))
            is_exception_or_too_many_request = False
            query = f'?startIndex={index}'
            url = self.ENDPOINT_NIST + query
            logging.info(url)
            logging.info(f'Request for {entries_for_request} entries from index={index}')
            try:
                response = requests.get(url, timeout=self.request_timeout)
                if response.status_code == 200:
                    logging.info(f'Data obtained for {entries_for_request} entries from index={index}')
                    response_json = response.json()
                    if response_json['startIndex'] >= response_json['totalResults']:
                        logging.info('Data up to date')
                        break
                    self.save_wrapper(response_json)
                    logging.info(f'Data saved for {entries_for_request} entries from index={index}')
                    index += entries_for_request
                    actual_retries = 0
                else:
                    logging.error(f'Request failed for index={index}, status code={response.status_code}')
                    is_exception_or_too_many_request = True
                    actual_retries += 1
            except Exception as e:
                logging.exception(e)
                is_exception_or_too_many_request = True
                actual_retries += 1
            if actual_retries == self.retries_for_request:
                logging.error(f'Maximum number of retries reached for index={index}, this request is skipped')
                with open(os.path.join(self.storage_path, self.MISSING_INDEXES), 'a') as f:
                    f.write(str(index) + '\n')
                logging.info(f'Missing index={index} saved into {self.MISSING_INDEXES}')
                actual_retries = 0
                index += entries_for_request
            if is_exception_or_too_many_request:
                logging.warning(
                    f'Going to sleep for {self.retry_interval} seconds due to too many requests or an exception')
                time.sleep(self.retry_interval)
            else:
                logging.info(f'Going to sleep for {self.interval_between_requests} seconds before the next request')
                time.sleep(self.interval_between_requests)
            logging.info('Crawler woke up')

    def save_wrapper(self, response_json):
        if self.mode == 'info':
            json_list = response_json['vulnerabilities']
            logging.info('Adding raw references to items')
        else:
            json_list = response_json['cveChanges']
        for e in json_list:
            complete_json = e
            if self.mode == 'info':
                complete_json = self.fetch_and_add_references(e)
            self.save_data(complete_json)

    def fetch_and_add_references(self, json_data):
        references = []
        try:
            for ref in json_data['cve']['references']:
                references.append(ref['url'])
            read_references = []
            ext_ref_id = 0
            for ref_url in references:
                try:
                    response = requests.get(ref_url, timeout=3, stream=True)
                    if response.status_code == 200:
                        content_length = response.headers.get('Content-Length')
                        content_type = response.headers.get('Content-Type', '')
                        is_textual = any(
                            kw in content_type for kw in ['text', 'json', 'xml', 'javascript', 'x-www-form-urlencoded'])

                        path = self.get_cve_path_and_filename(json_data)
                        if is_textual:
                            if content_length and int(content_length) > 5 * 1024 * 1024:
                                full_path = path + f'-{ext_ref_id}.txt'
                                with open(full_path, 'w', encoding=response.encoding or 'utf-8') as f:
                                    for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
                                        f.write(chunk)
                                read_references.append((ref_url, full_path))
                                ext_ref_id = ext_ref_id + 1
                            else:
                                read_references.append((ref_url, response.text))
                        else:
                            full_path = path + f'-{ext_ref_id}'
                            with open(full_path, 'wb') as f:
                                for chunk in response.iter_content(chunk_size=8192):
                                    f.write(chunk)
                            read_references.append((ref_url, full_path))
                            ext_ref_id = ext_ref_id + 1
                    else:
                        read_references.append((ref_url, response.status_code))
                except:
                    read_references.append((ref_url, 'Error with the request'))
            json_data['cve']['added_references'] = read_references
        except:
            pass
        return json_data

    def get_cve_path_and_filename(self, json_data):
        cve = json_data['cve']['id'] if self.mode == 'info' else json_data['change']['cveId']
        split_cve = cve.split('-')
        year = split_cve[1]
        cve_padded = str('{:06d}'.format(int(split_cve[2])))
        full_path = os.path.join(self.storage_path, year, cve_padded[:2], cve_padded[2:4])
        os.makedirs(full_path, exist_ok=True)
        return os.path.join(full_path, f'CVE-{year}-{cve_padded}')

    def save_data(self, json_data):
        try:
            path = self.get_cve_path_and_filename(json_data)
            if self.mode == 'info':
                with open(path + '.json', 'w') as file:
                    file.write(json.dumps(json_data))
            else:
                with open(path + '.jsonl', 'a') as file:
                    file.write(json.dumps(json_data) + '\n')
        except:
            raise RuntimeError('Cannot save data')

    def maintain_data(self):
        now = str(datetime.datetime.now().isoformat())
        try:
            with open(os.path.join(self.storage_path, self.LAST_UPDATE_FILENAME), 'r') as file:
                timestamp = file.read().strip()
        except FileNotFoundError:
            logging.info('No last timestamp detected, creating a new one with current time')
            with open(os.path.join(self.storage_path, self.LAST_UPDATE_FILENAME), 'w') as file:
                file.write(now)
            return
        logging.info(f'Request for update local data from {timestamp}')
        if self.mode == 'info':
            query = f'?lastModStartDate={timestamp}&lastModEndDate={now}'
        else:
            query = f'?changeStartDate={timestamp}&changeEndDate={now}'
        url = self.ENDPOINT_NIST + query
        try:
            response = requests.get(url, timeout=self.request_timeout)
            if response.status_code == 200:
                response_json = response.json()
                if self.mode == 'info':
                    key = 'vulnerabilities'
                else:
                    key = 'cveChanges'
                if response_json[key]:
                    if self.mode == 'info':
                        last_timestamp = response_json[key][-1]['cve']['lastModified']
                    else:
                        last_timestamp = response_json[key][-1]['change']['created']
                    logging.info(f'Data obtained from {timestamp} to {last_timestamp}')
                    self.save_wrapper(response_json)
                    logging.info(f'Data saved from {timestamp} to {last_timestamp}')
                else:
                    logging.info(f'Data up to date')
                    last_timestamp = now
                with open(os.path.join(self.storage_path, self.LAST_UPDATE_FILENAME), 'w') as file:
                    file.write(last_timestamp)
            else:
                logging.error(url)
                logging.error(f'Cannot obtain data from {timestamp}, status code={response.status_code}')
        except Exception as e:
            logging.exception(e)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CVE crawler')
    parser.add_argument('--mode', help='What to fetch of the CVEs: info or changes')

    args = parser.parse_args()
    CVECrawler(mode=args.mode).run()
