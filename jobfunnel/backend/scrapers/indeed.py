"""Scraper designed to get jobs from www.indeed.com / www.indeed.ca
"""
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, wait
import datetime
import logging
from math import ceil
from time import sleep, time
from typing import Dict, List, Tuple, Optional
import re
from requests import Session

from bs4 import BeautifulSoup

from jobfunnel.backend import Job, JobStatus
from jobfunnel.backend.localization import Locale, get_domain_from_locale
from jobfunnel.backend.scrapers import BaseScraper
from jobfunnel.config import SearchTerms


class BaseIndeedScraper(BaseScraper):
    """Scrapes jobs from www.indeed.X
    """
    def __init__(self, session: Session, search_terms: SearchTerms,
                 logger: logging.Logger) -> None:
        """Init that contains indeed specific stuff
        """
        self.session = session
        self.search_terms = search_terms
        self.logger = logger
        self.max_results_per_page = 50
        self.query = '+'.join(self.search_terms.keywords)

    def scrape(self) -> Dict[str, Job]:
        """Scrapes raw data from a job source into a list of Job objects

        Returns:
            List[Job]: list of jobs scraped from the job source
        """
        # Get the search url
        search = self.get_search_url()

        # Get the html data, initialize bs4 with lxml
        request_html = self.session.get(search)

        # Create the soup base
        soup_base = BeautifulSoup(request_html.text, self.bs4_parser)

        # Parse total results, and calculate the # of pages needed
        pages = self.get_num_pages_to_scrape(soup_base)
        self.logger.info(f"Found {pages} indeed results for query={self.query}")

        # Init list of job soups
        job_soup_list = []  # type: List[Any]

        # Init threads & futures list
        threads = ThreadPoolExecutor(max_workers=8)
        fts = []

        # Scrape soups for all the pages containing jobs it found
        for page in range(0, pages):
            # Append thread job future to futures list
            fts.append(
                threads.submit(
                    self.search_page_for_job_soups, search, page, job_soup_list
                )
            )

        # Wait for all scrape jobs to finish
        wait(fts)

        # make a dict of job postings from the listing briefs
        jobs_dict = {}  # type: Dict[str, Job]
        for s in job_soup_list:

            # init
            status = JobStatus.NEW
            title, company, location, tags = None, None, None, []
            post_date, key_id, url, short_description = None, None, None, None

            # Scrape the data for the post, requiring a minimum of info...
            try:
                # Jobs should at minimum have a title, company and location
                title = self.get_title(s)
                company = self.get_company(s)
                location = self.get_location(s)
                key_id = self.get_id(s)
                url = self.get_link(key_id)
            except AttributeError:
                self.logger.error("Unable to scrape minimum-required job info!")
                continue

            try:
                tags = self.get_tags(s)
            except AttributeError:
                self.logger.warning(f"Unable to scrape job tags for {key_id}")

            try:
                post_date = self.get_date(s)
            except AttributeError:
                self.logger.warning(
                    f"Unable to scrape job post date for {key_id}"
                )

            # Init a new job
            job = Job(
                title=title,
                company=company,
                location=location,
                description='',  # We will populate this later
                key_id=key_id,
                url=url,
                locale=self.locale,
                query=self.query,
                status=status,
                provider='indeed',  # FIXME: should inherit this?
                short_description=short_description,
                post_date=post_date,
                raw=s,
                tags=tags,
            )

            # FIXME: This doesn't work, and adding it would break existing csvs
            # try:
            #     self.set_short_description(job, s)
            # except AttributeError:
            #     self.logger.warning("Unable to scrape job short description.")

            # Fix the date to not be relative
            try:
                job.set_post_date_from_relative_date()
            except ValueError:
                self.logger.error(
                    f"Unknown date for job {key_id}, setting to epoch date."
                )
                job.post_date = datetime.datetime(1970, 1, 1)

            # Key by id to prevent duplicate key_ids TODO: add a warning
            jobs_dict[job.key_id] = job

        # FIXME: get the long descriptions
        return jobs_dict

    def convert_radius(self, radius: int) -> int:
        """function that quantizes the user input radius to a valid radius
           value: 5, 10, 15, 25, 50, 100, and 200 kilometers or miles
        """
        if radius < 5:
            radius = 0
        elif 5 <= radius < 10:
            radius = 5
        elif 10 <= radius < 15:
            radius = 10
        elif 15 <= radius < 25:
            radius = 15
        elif 25 <= radius < 50:
            radius = 25
        elif 50 <= radius < 100:
            radius = 50
        elif radius >= 100:
            radius = 100
        return radius

    @abstractmethod
    def get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        """
        pass

    @abstractmethod
    def get_link(self, job_id) -> str:
        """Constructs the link with the given job_id.
        Args:
			job_id: The id to be used to construct the link for this job.
        Returns:
                The constructed job link.
                Note that this function does not check the correctness of this link.
                The caller is responsible for checking correcteness.
        """
        pass

    def search_page_for_job_soups(self, search, page, job_soup_list):
        """Scrapes the indeed page for a list of job soups
        FIXME: types
        """
        url = f'{search}&start={int(page * self.max_results_per_page)}'
        self.logger.info(f'getting indeed page {page} : {url}')
        job_soup_list.extend(
            BeautifulSoup(
                self.session.get(url).text, self.bs4_parser
            ).find_all('div', attrs={'data-tn-component': 'organicJob'})
        )

    def get_full_description(self, job: Job) -> None:
        """Scrapes the indeed job link for the blurb and sets Job.short_desc
        """
        self.logger.info(f'getting indeed page: {job.url}')

        job_link_soup = BeautifulSoup(
            self.session.get(job.url).text, self.bs4_parser
        )
        try:
            job.short_description = job_link_soup.find(
                id='jobDescriptionText'
            ).text.strip()
        except AttributeError:
            self.logger.warning(f"Unable to load description for: {job.url}")
            job.short_description = ''
        job.clean_strings()

    def get_job_page_with_delay(self, job: Job,
                                delay: float) -> Tuple[Job, str]:
        """Gets data from the indeed job link and sets delays for requests
        """
        sleep(delay)
        self.logger.info(
            f'delay of {delay:.2f}s, getting indeed search: {job.url}'
        )
        return job, self.session.get(job.url).text


    def set_short_description(self, job: Job, soup: str) -> None:
        """Parses and stores job description from a job's page HTML
        FIXME: doesn't work. seems soup isn't right
        """
        job_link_soup = BeautifulSoup(soup, self.bs4_parser)
        try:
            job.description = job_link_soup.find(
                id='jobDescriptionText'
            ).text.strip()
        except AttributeError:
            job.description = ''
        job.clean_strings()

    def get_num_pages_to_scrape(self, soup_base, max_pages=0) -> int:
        """Calculates the number of pages to be scraped.
        Args:
			soup_base: a BeautifulSoup object with the html data.
    			At the moment this method assumes that the soup_base was
                prepared statically.
			max_pages: the maximum number of pages to be scraped.
        Returns:
            The number of pages to be scraped.
            If the number of pages that soup_base yields is higher than max,
            then max is returned.
        """
        num_res = soup_base.find(id='searchCountPages').contents[0].strip()
        num_res = int(re.findall(r'f (\d+) ', num_res.replace(',', ''))[0])
        number_of_pages = int(ceil(num_res / self.max_results_per_page))
        if max_pages == 0:
            return number_of_pages
        elif number_of_pages < max_pages:
            return number_of_pages
        else:
            return max_pages

    def get_title(self, soup) -> str:
        """Fetches the title from a BeautifulSoup base.
        Args:
			soup: BeautifulSoup base to scrape the title from.
        Returns:
            The job title scraped from soup.
            NOTE: that this function may throw an AttributeError if it cannot
            find the title. The caller is expected to handle this exception.
        """
        return soup.find(
            'a', attrs={'data-tn-element': 'jobTitle'}
        ).text.strip()

    def get_company(self, soup) -> str:
        """Fetches the company from a BeautifulSoup base.
        Args:
			soup: BeautifulSoup base to scrape the company from.
        Returns:
            The company scraped from soup.
            Note that this function may throw an AttributeError if it cannot
            find the company. The caller is expected to handle this exception.
        """
        return soup.find('span', attrs={'class': 'company'}).text.strip()

    def get_location(self, soup) -> str:
        """Fetches the job location from a BeautifulSoup base.
        Args:
			soup: BeautifulSoup base to scrape the location from.
        Returns:
            The job location scraped from soup.
            Note that this function may throw an AttributeError if it cannot
            find the location. The caller is expected to handle this exception.
        """
        return soup.find('span', attrs={'class': 'location'}).text.strip()

    def get_tags(self, soup) -> List[str]:
        """Fetches the job tags / keywords from a BeautifulSoup base.
        Args:
			soup: BeautifulSoup base to scrape the location from.
        Returns:
            The job location scraped from soup.
            Note that this function may throw an AttributeError if it cannot
            find the location. The caller is expected to handle this exception.
        """
        return [td.text.strip() for td in soup.find(
            'table', attrs={'class': 'jobCardShelfContainer'}
        ).find_all('td', attrs={'class': 'jobCardShelfItem'})]

    def get_date(self, soup) -> str:
        """Fetches the job date from a BeautifulSoup base.
        Args:
			soup: BeautifulSoup base to scrape the date from.
        Returns:
            The job date scraped from soup.
            Note that this function may throw an AttributeError if it cannot
            find the date. The caller is expected to handle this exception.
        """
        return soup.find('span', attrs={'class': 'date'}).text.strip()

    def get_id(self, soup) -> str:
        """Fetches the job id from a BeautifulSoup base.
        NOTE: this should be unique, but we should probably use our own SHA
        Args:
			soup: BeautifulSoup base to scrape the id from.
        Returns:
            The job id scraped from soup.
            Note that this function may throw an AttributeError if it cannot
            find the id. The caller is expected to handle this exception.
        """
        id_regex = re.compile(r'id=\"sj_([a-zA-Z0-9]*)\"')
        return id_regex.findall(
            str(soup.find('a', attrs={'class': 'sl resultLink save-job-link'}))
        )[0]


class IndeedScraperCAEng(BaseIndeedScraper):
    """Scrapes jobs from www.indeed.ca
    """
    @property
    def locale(self) -> Locale:
        return Locale.CANADA_ENGLISH

    @property
    def headers(self) -> Dict[str, str]:
        """Session header for Indeed
        """
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;'
            'q=0.9,image/webp,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate, sdch, br',
            'accept-language': 'en-GB,en-US;q=0.8,en;q=0.6',  # FIXME correct?
            'referer': 'https://www.indeed.{0}/'.format(
                get_domain_from_locale(self.locale)),
            'upgrade-insecure-requests': '1',
            'user-agent': self.user_agent,
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }

    def get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        """
        if method == 'get':
            # form job search url
            search = (
                "https://www.indeed.{0}/jobs?q={1}&l={2}%2C+{3}&radius={4}&"
                "limit={5}&filter={6}".format(
                    get_domain_from_locale(self.locale),
                    self.query,
                    self.search_terms.city.replace(' ', '+'),
                    self.search_terms.province,
                    self.convert_radius(self.search_terms.radius),
                    self.max_results_per_page,
                    int(self.search_terms.return_similar_results)
                )
            )
            return search
        elif method == 'post':
            # TODO: implement post style for indeed.X
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')

    def get_link(self, job_id) -> str:
        """Constructs the link with the given job_id.
        Args:
			job_id: The id to be used to construct the link for this job.
        Returns:
                The constructed job link.
                Note that this function does not check the correctness of this link.
                The caller is responsible for checking correcteness.
        """
        return (f"http://www.indeed.{get_domain_from_locale(self.locale)}"
                f"/viewjob?jk={job_id}"
        )

# TODO: IndeedScraperCAFr

class IndeedScraperUSAEng(BaseIndeedScraper):
    """Scrapes jobs from www.indeed.com
    """
    @property
    def locale(self) -> Locale:
        return Locale.USA_ENGLISH

    @property
    def headers(self) -> Dict[str, str]:
        """Session header for Indeed
        """
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;'
            'q=0.9,image/webp,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate, sdch, br',
            'accept-language': 'en-US;q=0.8,en;q=0.6',
            'referer': 'https://www.indeed.{0}/'.format(
                get_domain_from_locale(self.locale)),
            'upgrade-insecure-requests': '1',
            'user-agent': self.user_agent,
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }

    def get_search_url(self, method: Optional[str] = 'get') -> str:
        """Get the indeed search url from SearchTerms
        """
        if method == 'get':
            # form job search url
            search = (
                "https://www.indeed.{0}/jobs?q={1}&l={2}%2C+{3}&radius={4}&"
                "limit={5}&filter={6}".format(
                    get_domain_from_locale(self.locale),
                    self.query,
                    self.search_terms.city.replace(' ', '+'),
                    self.search_terms.state,
                    self.convert_radius(self.search_terms.region.radius),
                    self.max_results_per_page,
                    int(self.search_terms.return_similar_results)
                )
            )
            return search
        elif method == 'post':
            # TODO: implement post style for indeed.X
            raise NotImplementedError()
        else:
            raise ValueError(f'No html method {method} exists')

    def get_link(self, job_id) -> str:
        """Constructs the link with the given job_id.
        Args:
			job_id: The id to be used to construct the link for this job.
        Returns:
                The constructed job link.
                Note that this function does not check the correctness of this link.
                The caller is responsible for checking correcteness.
        """
        return (f"http://www.indeed.{get_domain_from_locale(self.locale)}"
                f"/viewjob?jk={job_id}"
        )