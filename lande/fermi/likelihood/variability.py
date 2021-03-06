""" Code to compute the variability of a source
    using pointlike. 

    Author: Joshua Lande <joshualande@gmail.com>
"""
# N.B. Have to import gtlike stuff first
from roi_gtlike import Gtlike
from SED import SED

from tempfile import mkdtemp
from os.path import join, exists, expandvars
import os
import shutil

import yaml
 
import pylab as P
import pyfits
import numpy as np
from scipy import stats
from scipy.optimize import fmin

from GtApp import GtApp

from uw.utilities import keyword_options

from uw.like.pointspec import DataSpecification, SpectralAnalysis
from uw.like.roi_state import PointlikeState
from uw.utilities.phasetools import phase_ltcube

from roi_gtlike import Gtlike

from lande.utilities.tools import tolist
from lande.utilities.plotting import plot_points

from . fit import paranoid_gtlike_fit
from . modify import modify
from . save import flux_dict, diffuse_dict, get_background, get_sources, source_dict
from . limits import GtlikeUpperLimit, PointlikeUpperLimit
from . fit import allow_fit_only_prefactor
from . superstate import SuperState
from . basefit import BaseFitter
from . printing import summary

from lande.fermi.data.livetime import pointlike_ltcube


class VariabilityTester(BaseFitter):

    def plot(self, 
             filename=None,
             axes=None,
             figsize=(7,4), 
             plot_gtlike=True,
             plot_pointlike=True,
             gtlike_color='black',
             pointlike_color='red',
             **kwargs):
        """ Create a plot from a dictionary. """

        bands = self.results['bands']
        all_time = self.results['all_time']
        min_ts = self.results['min_ts']


        if axes is None:
            fig = P.figure(None,figsize)
            axes = fig.add_subplot(111)

        # astype(float) converts None to nan
        a=lambda x: np.asarray(x).astype(float)

        starts=a(self.results['time']['starts'])
        stops=a(self.results['time']['stops'])
        time=(starts+stops)/2.0

        loop = [ ]
        if plot_gtlike:
            loop.append(['gtlike',gtlike_color],)
        if plot_pointlike:
            loop.append(['pointlike',pointlike_color])

        for t,color in loop:

            f0 = all_time[t]['flux']['flux']


            ts = a([b[t]['TS'] for b in bands])

            significant = (ts >= min_ts)

            f = a([b[t]['flux']['flux'] for b in bands])
            ferr=a([b[t]['flux']['flux_err'] for b in bands])
            ferr=a([b[t]['flux']['flux_err'] for b in bands])

            fup=a([b[t]['upper_limit']['flux'] if b[t]['upper_limit'] is not None else None for b in bands])

            plot_points(
                axes=axes,
                x=time,
                xlo=starts,
                xhi=stops,
                y=f,
                y_lower_err=ferr,
                y_upper_err=ferr,
                y_ul=fup,
                significant=significant,
                color=color,
                label=t,
                **kwargs)
            
            axes.axhline(f0, color=color, dashes=[5,2])

        axes.set_xlabel('MET (s)')
        axes.set_ylabel('Flux (ph$\,$cm$^{-2}$s$^{-1}$)')
        axes.legend()

        if filename is not None: 
            P.savefig(expandvars(filename))
        return axes

class CombinedVariabilityTester(VariabilityTester):
    """ Code to compute varability for for a pointlike ROI
        using the method described in 2FGL:

            arXiv:1108.1435v1

        A nice description of this method is:

            https://confluence.slac.stanford.edu/display/SCIGRPS/How+to+-+Variability+test
    """

    f = 0.02

    defaults = BaseFitter.defaults + (
        ("savedir",               None, """ Directory to put output files into. 
                                            Default is to use a temporary file and 
                                            delete it when done."""),
        ("savedir_prefix", '/scratch/', "Directory to put tempdir in."),
        ("always_upper_limit",   False, """ Always compute an upper limit. """),
        ("min_ts",                   4, """ minimum ts in which to quote a SED points instead of an upper limit."""),
        ("ul_confidence",         0.95, """ confidence level for upper limit."""),
        ("gtlike_kwargs",           {}, """ Kwargs to specify creating of gtlike object. """),
        ("do_gtlike",             True, """ Run gtlike varaibility test. """),
        ("refit_background",      True, """ Fit the background sources in each energy bin."""),
        ("refit_other_sources",   True, """ Fit other sources in each energy bin. """),
        ("use_pointlike_ltcube", False, """ Make the ltcubes with pointlike. """),
        ("nbins",                 None, """ Specify the number of time bins (the time range is
                                            taken from the ft1 file)"""),
        ("tstarts",               None, """ Specify an array of start times. """),
        ("tstops",                None, """ Specify an array of stop times. """),
    )

    @keyword_options.decorate(defaults)
    def __init__(self, roi, name, *args, **kwargs):
        self.roi = roi
        keyword_options.process(self, kwargs)

        self.pointlike_fit_kwargs = dict(use_gradient=False)

        self.name = name

        self._setup_savedir()

        self._setup_time_bins()

        saved_state = PointlikeState(roi)

        self._test_variability()

        saved_state.restore()


    def _setup_savedir(self):
        if self.savedir is not None:
            self.save_data = True
            if not exists(self.savedir):
                os.makedirs(self.savedir)
        else:
            self.save_data = False
            self.savedir=mkdtemp(prefix=self.savedir_prefix)


    def _setup_time_bins(self):
        roi = self.roi

        if self.nbins is None and (self.tstarts is None and self.tstops is None):
            raise Exception("Must specify nbins or tstarts and tstops")
        if self.nbins is not None and (self.tstarts is not None and self.tstops is not None):
            raise Exception("Must not specify both nbins or tstarts and tstops")
        if self.tstarts is None and self.tstops is not None or self.tstarts is not None and self.tstops is None:
            raise Exception("Must specify both tsarts and tstops")

        # Divide into several time bins
        ft1files=roi.sa.pixeldata.ft1files
        self.earliest_time, self.latest_time = CombinedVariabilityTester.get_time_range(ft1files)

        self.time = dict()
        if self.nbins is not None:
            # round to nearest second, for simplicity
            b = np.round(np.linspace(self.earliest_time, self.latest_time, self.nbins+1)).astype(int)
            starts = b[:-1]
            stops = b[1:]
        elif self.tstarts is not None and self.tstops is not None:
            assert len(self.tstarts) == len(self.tstops)
            self.nbins = len(self.tstarts)
            starts = self.tstarts
            stops = self.tstops

        self.time['starts']  = np.asarray(starts, dtype=int)
        self.time['stops'] = np.asarray(stops, dtype=int)

        if self.verbosity:
            print 'variability time starts:',self.time['starts']
            print 'variability time stops:',self.time['stops']

    def all_time_fit(self):
        if self.verbosity: print 'First, computing best all-time parameters'

        roi = self.roi
        name = self.name

        # first, spectral fit in gtlike

        if self.verbosity: print 'Performing pointlike spectral analyis over all energy'
        if self.verbosity: print '... Before'; roi.print_summary()
        roi.fit(**self.pointlike_fit_kwargs)
        if self.verbosity: print '... After'; roi.print_summary()

        all_time = dict(pointlike = source_dict(roi, name))

        all_time['pointlike']['TS'] = roi.TS(name,quick=False)

        if self.do_gtlike:

            savedir = join(self.savedir,'all_time') if self.save_data else None

            gtlike=Gtlike(roi, 
                          savedir=savedir,
                          **self.gtlike_kwargs)
            like=gtlike.like

            if self.verbosity: 
                print 'Performing gtlike spectral analyis over all energy'
                print '... Before'; print summary(like)
            paranoid_gtlike_fit(like, verbosity=self.verbosity)
            if self.verbosity: 
                print '... After'; print summary(like)

            self.best_gtlike_state = SuperState(like)

            all_time['gtlike'] = source_dict(like, name)

        return all_time


    def each_time_fit_pointlike(self, smaller_roi, tstart, tstop):

        results = dict()

        if self.verbosity: print 'Performing Pointlike analysis from %s to %s' % (tstart, tstop)
        name=self.name

        ll = lambda: -smaller_roi.logLikelihood(smaller_roi.parameters())

        if not self.refit_background:
            for source in get_background(smaller_roi):
                    roi.modify(which=source,free=False)

        if not self.refit_other_sources:
            for source in get_sources(smaller_roi):
                smaller_roi.modify(which=source,free=False)

        # Freeze source of interest
        smaller_roi.modify(which=name, free=False)

        if self.verbosity: print 'Performing pointlike spectral analyis with source of interest frozen'

        if self.verbosity: 
            print '... Before'; smaller_roi.print_summary()
        smaller_roi.fit(**self.pointlike_fit_kwargs)
        if self.verbosity: 
            print '... After'; smaller_roi.print_summary()

        results['ll_0'] = ll()

        # Fit prefactor of source of interest
        allow_fit_only_prefactor(smaller_roi, name)

        if self.verbosity: print "Performing pointlike spectral analyis with source of interest's prefactor free"

        # * fit prefactor of source
        if self.verbosity: 
            print '... Before'; smaller_roi.print_summary()
        smaller_roi.fit(**self.pointlike_fit_kwargs)
        if self.verbosity: 
            print '... After'; smaller_roi.print_summary()

        # * calcualte likelihood for the fit flux
        results['ll_1'] = ll()
        results['flux'] = flux_dict(smaller_roi,name)
        results['TS'] = TS = smaller_roi.TS(name,quick=False)
        results['diffuse'] = diffuse_dict(smaller_roi)


        if TS < self.min_ts or self.always_upper_limit:
            pul=PointlikeUpperLimit(smaller_roi, name, cl=0.95, verbosity=self.verbosity)
            results['upper_limit'] = pul.todict()
        else:
            results['upper_limit'] = None

        return results

    def each_time_fit_gtlike(self, smaller_roi, tstart, tstop, subdir):

        if self.verbosity: print 'Performing gtlike analysis from %s to %s' % (tstart, tstop)

        name=self.name

        results = dict()

        gtlike=Gtlike(smaller_roi, 
                      savedir=subdir,
                      **self.gtlike_kwargs)
        like=gtlike.like

        ll = lambda: like.logLike.value()

        # update gtlike object with best fit gtlike parameters
        self.best_gtlike_state.restore(like)

        if not self.refit_background:
            for source in get_background(like):
                modify(like, source, free=False)

        if not self.refit_other_sources:
            for source in get_sources(like):
                modify(like,source, free=False)

        if self.verbosity: print 'Performing gtlike spectral analyis with source of interest frozen'

        def p(x):
            if self.verbosity: 
                print '... %s' % x; 
                print summary(like)

        # Freeze source of interest
        modify(like, name, free=False)

        p('Before')
        paranoid_gtlike_fit(like, verbosity=self.verbosity)
        p('After')

        results['ll_0'] = ll()

        # Fit prefactor of source of interest
        allow_fit_only_prefactor(like, name)

        if self.verbosity: print "Performing gtlike spectral analyis with source of interest's prefactor free"

        p('Before')
        paranoid_gtlike_fit(like, verbosity=self.verbosity)
        p('After')

        results['ll_1'] = ll()
        results['flux'] = flux_dict(like,name)

        results['TS'] = TS = like.Ts(name,reoptimize=True,verbosity=self.verbosity)
        results['diffuse'] = diffuse_dict(like)

        if TS < self.min_ts or self.always_upper_limit:
            if self.verbosity: 
                print 'Compute Gtlike Upper Limit'
            gul = GtlikeUpperLimit(like, name, cl=0.95, verbosity=self.verbosity)
            results['upper_limit'] = gul.todict()
        else:
            results['upper_limit'] = None

        return results

    def _test_variability(self):
        roi = self.roi

        # Perform all-time analysis
        self.all_time = self.all_time_fit()

        self.bands = []

        for i,(tstart,tstop) in enumerate(zip(self.time['starts'], self.time['stops'])):

            subdir = join(self.savedir,'time_%s_%s' % (tstart, tstop))
            if self.verbosity: print 'Subdir = ',subdir
            if not exists(subdir):
                os.makedirs(subdir)

            days = (tstop-tstart)/(60*60*24)
            if self.verbosity: print  '%s/%s Looping from time %s to %s (%.1f days)' % (i+1, self.nbins, tstart, tstop, days)

            band = dict(
                tstart = tstart, 
                tstop = tstop,
                days = days)

            self.bands.append(band)

            smaller_roi = CombinedVariabilityTester.time_cut(roi, tstart, tstop, subdir, self.use_pointlike_ltcube, self.verbosity)

            band['pointlike'] = self.each_time_fit_pointlike(smaller_roi, tstart, tstop)
            if self.do_gtlike:
                band['gtlike'] = self.each_time_fit_gtlike(smaller_roi, tstart, tstop, subdir)

            if not self.save_data:
                if self.verbosity: print 'Removing subdir',subdir
                shutil.rmtree(subdir)

        self.TS_var = dict(
            pointlike = self.compute_TS_var('pointlike')
        )
        if self.do_gtlike:
            self.TS_var['gtlike'] = self.compute_TS_var('gtlike')

        self.results = dict(
            name = self.name,
            min_ts = self.min_ts,
            time = self.time,
            bands = self.bands,
            all_time = self.all_time,
            TS_var = self.TS_var)

    def compute_TS_var(self,type):
        a=np.asarray
        ll1 = a([b[type]['ll_1'] for b in self.bands])
        ll0 = a([b[type]['ll_0'] for b in self.bands])

        f = self.f

        F = a([b[type]['flux']['flux'] for b in self.bands])
        F0 = self.all_time[type]['flux']['flux']

        df = F - F0
        TS_var = 2*(ll1-ll0)

        fraction = df**2/(df**2 + f**2*F0**2)

        # Sometimes flux can e nan, in which case do not scale 
        # by this term.
        good_fraction = np.where(~np.isnan(fraction), fraction, 1)

        TS_var *= good_fraction

        return np.sum(TS_var)

    @staticmethod
    def get_time_range(ft1files):
        """ Get the largest time range (in MET) from an ft1 file
            or a list of ft1 files. """
        if isinstance(ft1files,list):
            tmins,tmaxs = zip(*[CombinedVariabilityTester.get_time_range(i) for i in ft1files])
            return min(tmins),max(tmaxs)

        f=pyfits.open(ft1files)
        tmin = min(f[2].data.field('START'))
        tmax = max(f[2].data.field('STOP'))
        return tmin, tmax

    @staticmethod
    def time_cut(roi, tstart, tstop, subdir, use_pointlike_ltcube, verbosity):
        """ Create a new ROI given a time cut. """

        sa = roi.sa
        ds = sa.dataspec

        get_defaults=lambda obj: [k[0] for k in obj.defaults if not isinstance(k,str)]
        get_kwargs=lambda obj: {k:obj.__dict__[k] for k in get_defaults(obj)}

        ds_kwargs, sa_kwargs, roi_kwargs = map(get_kwargs,[ds,sa,roi])

        point_sources = [i.copy() for i in roi.psm.point_sources]
        diffuse_sources = [i.copy() for i in roi.dsm.diffuse_sources]

        if sa_kwargs['tstart'] !=0 or sa_kwargs['tstop'] !=0:
            raise Exception("sanity check")

        # * cut ft1file on time using gtselect

        ft2files=roi.sa.pixeldata.ft2files
        if len(ft2files) > 1: raise Exception("...")
        ft2file=ft2files[0]

        evfile=Gtlike.make_evfile(roi,subdir)

        cut_evfile=join(subdir,"cut_ft1_%s_%s.fits" % (tstart, tstop))

        if not exists(cut_evfile):
            if verbosity: print 'Running gtselect'
            gtselect=GtApp('gtselect', 'dataSubselector')
            gtselect.run(infile=evfile,
                         outfile=cut_evfile,
                         ra=0, dec=0, rad=180,
                         tmin=tstart, tmax=tstop,
                         emin=1, emax=1e7,
                         zmax=180)

        else:
            if verbosity: print '... Skiping gtselect for %s to %s' % (tstart,tstop)

        ds_kwargs['ft1files'] = cut_evfile

        # * create new binfile and ltcube
        ds_kwargs['binfile'] = join(subdir,'binned_%s_%s.fits' % (tstart, tstop))

        # save this to see if it has been phased by 
        # the function uw.utilities.phasetools.phase_ltcube
        all_time_ltcube = ds_kwargs['ltcube']

        new_ltcube = join(subdir,'ltcube_%s_%s.fits' % (tstart, tstop))

        if not exists(new_ltcube):
            if verbosity: print 'Running gtltcube for %s to %s' % (tstart,tstop)

            if use_pointlike_ltcube:
                pointlike_ltcube(evfile=cut_evfile,
                                 scfile=ft2file,
                                 outfile=new_ltcube,
                                 dcostheta=0.025, 
                                 binsz=1,
                                 zmax=roi.sa.zenithcut,
                                 cone_angle=roi.sa.exp_radius,
                                 dir=roi.roi_dir)
            else:
                gtltcube=GtApp('gtltcube', 'Likelihood')
                gtltcube.run(evfile=cut_evfile,
                             scfile=ft2file,
                             outfile=new_ltcube,
                             dcostheta=0.025, 
                             binsz=1)
        else:
            if verbosity: print '... Skiping gtltcube for %s to %s' % (tstart,tstop)

        # next, check if ltcube is phased, kind of a kluge
        f = pyfits.open(all_time_ltcube)
        if f['exposure'].header.has_key('PHASE'):
            assert f['exposure'].header['PHASE'] == f['weighted_exposure'].header['PHASE']
            # If so, phase new ltcube
            phase = f['exposure'].header['PHASE']

            phased_ltcube = join(subdir,'phased_ltcube_%s_%s.fits' % (tstart, tstop))
            if not exists(phased_ltcube):
                phase_ltcube(new_ltcube, phased_ltcube, phase)
            else:
                if verbosity: print '... Skiping gtltcube phasing for %s to %s' % (tstart,tstop)

            ds_kwargs['ltcube'] = phased_ltcube
        else:
            ds_kwargs['ltcube'] = new_ltcube

        # * create new ds, sa, and roi
        new_ds = DataSpecification(**ds_kwargs)
        sa = SpectralAnalysis(new_ds, **sa_kwargs)
        return sa.roi(
            point_sources = point_sources,
            diffuse_sources = diffuse_sources,
            **roi_kwargs)



    @staticmethod
    def _TS_to_sigma(ts, nbins):
        """ Followign the 2FGL discussion of TS_var,

            TS should have a chi^2/2 distribution
            with nbins-1 degrees of freedom.

            The survival function of the chi^2 distribution
            is the false detection rate (remember the
            survival function is 1-cdf).

            By figuring out at what value the survival function of twice
            the (normalized) normal distribution equals the above survival function of
            the chi^2 distribution, we can figure out the 'sigma' of
            the detection.

            Note, this is a two sided significance, which (I believe)
            is more suitable for variability since it avoids obtaining
            negative TS values


            This code is equivalent to the mathematica function:

            NSolve[
                SurvivalFunction[ChiSquareDistribution[nbins-1], ts] == 
                SurvivalFunction[2*NormalDistribution[0, 1], y], y
            ]


            Assuming 36 time bins, I precomputed with mathematica that

                >>> sigma = CombinedVariabilityTester._TS_to_sigma
                >>> print '%.1f' % sigma(50, 36)
                2.0
                >>> print '%.1f' % sigma(30, 24)
                1.4

            Since we compute the (2 sided) significance, the smallest sigma
            we can get is 0 (for TSvar=0)

                >>> print '%.1f' % sigma(0, 24)
                0.0

            This is a 5 sigma detection (with 36 time bins)

                >>> print '%.1f' % sigma(91.6638, 36)
                5.0

            Now, lest see how far we can push the limits of the float precision

                >>> print '%.1f' % sigma(200, 36)
                10.3

                >>> print '%.1f' % sigma(800, 36)
                25.7

            Our function fails to provide meaningful
            statstical significances larger than this.

            Fortunately, Crab has TS_var ~ 600 so
            we don't have to worry about these outliers

        """
        chidist = stats.chi2(nbins-1)

        # This is 1 minus the fase detection probability
        prob = chidist.sf(ts)

        normdist = stats.norm(loc=0, scale=1)

        # Try to solve sf(norm)(i) = prob for i
        # by minimizing the difference
        f = lambda i: (2*normdist.sf(i) - prob)**2

        # 1 sigma is a reasonable guess
        sigma = fmin(f,[1], xtol=1e-10000, ftol=1e-10000, 
                     full_output=False, disp=False)[0]
        return sigma


    def __del__(self):
        if not self.save_data:
            if not self.verbosity: print 'Removing savedir',self.savedir
            shutil.rmtree(self.savedir)


if __name__ == "__main__":
    import doctest
    doctest.testmod()
