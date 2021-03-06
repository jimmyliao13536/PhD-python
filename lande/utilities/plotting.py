from os.path import expandvars
from collections import Iterable
import copy

import numpy as np

from matplotlib.figure import Figure
from mpl_toolkits.axes_grid.anchored_artists import AnchoredText
from matplotlib.patheffects import withStroke
from mpl_toolkits.axes_grid1.axes_grid import Grid, AxesGrid, ImageGrid
import matplotlib.lines as mlines
import matplotlib.transforms as mtransforms 


from . arrays import nzip

def histpoints(data, bins, *args, **kwargs):
    """ Create a series of x,y, points which will draw a histogram.
        Useful because the points can be directly plotted with
        the plot function. This is similar to the matplotlib hist
        function, but allows more direct control. 
        
            >>> data = [0.25, 0.5, 1.25]
            >>> bins = [0,1,2]
            >>> x,y=histpoints(data, bins)
            >>> np.all(x == [0, 0, 1, 1, 2, 2])
            True
            >>> np.all(y == [0, 2, 2, 1, 1, 0])
            True
    """
    binned_data, bins = np.histogram(data, bins=bins, *args, **kwargs)
    return binned_data_and_bins_to_x_y(binned_data,bins)

def binned_data_and_bins_to_x_y(binned_data,bins):
    x = nzip(bins, bins)
    y = np.concatenate(([0], nzip(binned_data, binned_data), [0]))
    return x,y

def plot_ds9_contour(ax,contour,**kwargs):
    """ Parse a ds9 format contour file. Kwargs goes into the plot function. """
    lines=open(expandvars(contour)).readlines()
    ras,decs=[],[]
    for line in lines:
        if line.strip() is '':
            ax['fk5'].plot(ras,decs,'-',**kwargs)
            ras,decs=[],[]
        else:
            ra,dec=line.split()
            ras.append(float(ra)); decs.append(float(dec))


def get_major_ticks_within_view_interval(axis): 
    """ Function shamelessly taken from:
            http://www.nabble.com/eliminating-the-top-tick-on-an-axis-to19446256.html#a19446256

        but the return now is the locations, not the ticks
        which seems to work better with AxesGrids.
    """
    interval = axis.get_view_interval() 

    locs = [] 
    for tick, loc in zip(axis.get_major_ticks(), 
                         axis.get_major_locator()()): 
        if mtransforms.interval_contains(interval, loc): 
            locs.append(loc) 
    return locs


def fix_xaxesgrid(grid):
    """ When you create an axesgrid which with no gap between the x and y plots,
        there is always an overlaping tick. Remove the ticks which overlap with nearby axes. """
    if grid._direction != 'row': raise Exception("Not implemented")
    if grid._refax is not None: raise Exception("This function does not work for AxesGrids with share_all=True")

    nrows,ncols=grid._nrows,grid._ncols

    for row in range(nrows):
        for col in range(ncols):
            ax = grid[row*ncols + col]
            if col != ncols-1 and row==nrows-1:
                ticks=get_major_ticks_within_view_interval(ax.xaxis)
                ax.set_xticks(ticks[:-1])

def fix_yaxesgrid(grid):
    """ When you create an axesgrid which with no gap between the x and y plots,
        there is always an overlaping tick. Remove the ticks which overlap with nearby axes. """
    if grid._direction != 'row': raise Exception("Not implemented")
    if grid._refax is not None: raise Exception("This function does not work for AxesGrids with share_all=True")

    nrows,ncols=grid._nrows,grid._ncols

    for row in range(nrows):
        for col in range(ncols):
            ax = grid[row*ncols + col]
            if row != 0 and col==0:
                ticks=get_major_ticks_within_view_interval(ax.yaxis)
                ax.set_yticks(ticks[:-1])

def fix_axesgrid(grid):
    fix_xaxesgrid(grid)
    fix_yaxesgrid(grid)


def label_axes(plots, stroke=True, **kwargs):
    """ Add "(a)" to first plot, "(b)" to second, ... """

    text_kwargs=dict(frameon=False, loc=2, prop=dict(size=14))
    text_kwargs.update(kwargs)

    if isinstance(plots, Iterable) or isinstance(plots, Grid) or \
       isinstance(plots, AxesGrid) or isinstance(plots, ImageGrid):
        plot_list=plots
    elif isinstance(plots,Figure):
        plot_list=plots.axes
    else:
        raise Exception("Unrecognized plot list.")

    for i,g in enumerate(plot_list):
        _at = AnchoredText('(%s)' % chr(i+97), **text_kwargs)

        if stroke:
            _at.txt._text.set_path_effects([withStroke(foreground="w", linewidth=3)])

        g.add_artist(_at)


def plot_points(x, y, xlo, xhi,
                y_lower_err, y_upper_err, y_ul, significant,
                axes,
                ul_fraction=0.4,
                **kwargs):
        """ Plot data points with errors using matplotlib.

        if no x errors, set xlo=xhi=None. 
        """
        plot_kwargs = dict(linestyle='none', capsize=0)
        plot_kwargs.update(kwargs)

        if isinstance(significant,bool):
            significant = np.asarray([significant]*len(x),dtype=bool)

        s = significant

        if xhi is not None:
            dx_hi=xhi-x
        else:
            dx_hi=np.zeros_like(y, dtype=float)

        if xlo is not None:
            dx_lo=x-xlo
        else:
            dx_lo=np.zeros_like(x)

        if y_lower_err is None:
            y_lower_err = np.zeros_like(y, dtype=float)
        else:
            y_lower_err = y_lower_err.copy()

        if y_upper_err is None:
            y_upper_err = np.zeros_like(y, dtype=float)
        else:
            y_upper_err = y_upper_err.copy()

        if sum(~s)>0:
            # If there are upper limits, replace data points in arrays
            # with upper limits.
            y[~s] = y_ul[~s]
            y_lower_err[~s]  = ul_fraction*y_ul[~s]
            y_upper_err[~s]  = np.zeros(sum(~s), dtype=float)

        # plot data points
        axes.errorbar(x, y,
                      xerr=[dx_lo, dx_hi], yerr=[y_lower_err, y_upper_err],
                      **plot_kwargs)

        # and upper limits
        if sum(~s)>0:
            if 'label' in plot_kwargs: plot_kwargs.pop('label')

            for k in ['capsize', 'elinewidth', 'marker']:
                if k in plot_kwargs:
                    plot_kwargs.pop(k)

            # plot the upper limit down arrow markers
            axes.plot(x[~s], (1-ul_fraction)*y_ul[~s],
                      marker=mlines.CARETDOWN,
                      **plot_kwargs)



if __name__ == "__main__":
    import doctest
    doctest.testmod()
