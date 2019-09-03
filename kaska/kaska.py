# -*- coding: utf-8 -*-

"""Main module."""
import logging

import datetime as dt
import numpy as np

from scipy.interpolate import interp1d

from .NNParameterInversion import NNParameterInversion

from .s2_observations import Sentinel2Observations

from s1_observations import Sentinel1Observations

from smoothn import smoothn

LOG = logging.getLogger(__name__ + ".KaSKA")
LOG.setLevel(logging.DEBUG)
if not LOG.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - ' +
                                  '%(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    LOG.addHandler(ch)
LOG.propagate = False

            
def define_temporal_grid(start_date, end_date, temporal_grid_space):
    """Creates a temporal grid"""
    temporal_grid = [start_date + i*dt.timedelta(days=temporal_grid_space) 
                    for i in range(int(np.ceil(366/temporal_grid_space)))
                    if start_date + i*dt.timedelta(days=temporal_grid_space)
                                    <= end_date]
    return temporal_grid

class KaSKA(object):
    """The main KaSKA object"""

    def __init__(self, observations, time_grid, state_mask, approx_inverter,
                output_folder):
        self.time_grid = time_grid
        self.observations = observations
        self.state_mask = state_mask
        self.output_folder = output_folder
        self.inverter = NNParameterInversion(approx_inverter)

    def first_pass_inversion(self):
        """A first pass inversion. Could be anything, from a quick'n'dirty
        LUT, a regressor. As coded, we use the `self.inverter` method, which
        in this case, will call the ANN inversion."""
        S = {}
        for k in self.observations.dates:
            retval = self.inverter.invert_observations(self.observations, k)
            if retval is not None:
                S[k] = retval
        return S

    def _process_first_pass(self, first_passer_dict):
        """This methods takes the first pass estimates of surface parameters
        (stored as a dictionary) and assembles them into an
        `(n_params, n_times, nx, ny)` grid. The assumption here is the 
        dictionary is indexed by dates (e.g. datetime objects) and that for
        each date, we have a list of parameters.
        
        Parameters
        ----------
        first_passer_dict: dict
            A dictionary with first pass guesses in an irregular temporal grid
        
        """
        dates = [k for k in first_passer_dict.keys()]
        n_params, nx, ny = first_passer_dict[dates[0]].shape
        param_grid = np.zeros((n_params, len(dates), nx, ny))
        for i, k in enumerate(dates):
            for j in range(n_params):
                    param_grid[j, i, :, :] = first_passer_dict[k][j]
        # param_grid = np.zeros((n_params, len(self.time_grid), nx, ny))
        # idx = np.argmin(np.abs(self.time_grid -
        #                 np.array(dates)[:, None]), axis=1)
        # LOG.info("Re-arranging first pass solutions into an array")
        # for ii, tstep in enumerate(self.time_grid):
        #     ## Number of observations in current time step
        #     #n_obs_tstep = list(idx).count(ii)
        #     # Keys for the current time step
        #     sel_keys = list(np.array(dates)[idx == ii])
        #     LOG.info(f"Doing timestep {str(tstep):s}")
        #     for k in sel_keys:
        #         LOG.info(f"\t {str(k):s}")
        #     for p in range(n_params):
        #         arr = np.array([first_passer_dict[k][p] for k in sel_keys])
        #         arr[arr < 0] = np.nan
        #         param_grid[p, ii, :, :] = np.nanmean(arr, axis=0)
        return dates, param_grid

    def run_retrieval(self):
        """Runs the retrieval for all time-steps. It proceeds by first 
        inverting on a observation by observation fashion, and then performs
        a per pixel smoothing/interpolation."""
        dates, retval = self._process_first_pass(self.first_pass_inversion())
        LOG.info("Burp! Now doing temporal smoothing")
        return self._run_smoother(dates, retval)
        #x0 = np.zeros_like(retval)
        #for param in range(retval.shape[0]):
        #    S = retval[param]*1
        #    ss = smoothn(S, isrobust=True, s=1, TolZ=1e-2, axis=0)
        #    x0[param, :, :] = ss[0]
        #return x0

    def _run_smoother(self, dates, parameter_block):
        """Very specific method that applies some parameter transformations
        to the data in a very unrobust way."""
        # This needs to be abstracted up...
        # Note that in general, we don't know what parameters we are dealing
        # with. We probably want a data structure here with the parameter list,
        # transformation function, as well as boundaries, which could be
        # associated with the NN
        lai = -2 * np.log(parameter_block[-2, :, :, :])
        cab = -100*np.log(parameter_block[1, :, :, :])
        cbrown = parameter_block[2, :, :, :]
        # Basically, remove weird values outside of boundaries, nans and stuff
        # Could be done simply with the previously stated data structure, as
        # this is a bit of an adhoc piece of code.
        lai[~np.isfinite(lai)] = 0
        cab[~np.isfinite(cab)] = 0
        cbrown[~np.isfinite(cbrown)] = 0
        lai[~(lai > 0)] = 0
        cab[~(cab > 0)] = 0
        cbrown[~(cbrown > 0)] = 0
        # Create a mask where we have no (LAI) data
        mask = np.all(lai == 0, axis=(0))
        LOG.info("Smoothing data like a boss")
        # Time axes in days of year
        doys = np.array([int(x.strftime('%j')) for x in dates])
        doy_grid = np.array([int(x.strftime('%j')) for x in self.time_grid])
        # Linear 3D stack interpolator. Assuming dimension 0 is time. Note use
        # of fill_value to indicate missing data (0)
        f = interp1d(doys, lai, axis=0, bounds_error=False,
                     fill_value=0)
        laii = f(doy_grid)
        slai = smoothn(np.array(laii), W=np.array(laii), isrobust=True, s=1,
                       TolZ=1e-6, axis=0)[0]
        slai[slai < 0] = 0
        # The last bit is to fix LAI to 0
        # going forward, use LAI as weighting to try to dampen flappiness in
        # pigments when no leaf area is present.
        f = interp1d(doys, cab, axis=0, bounds_error=False)
        cabi = f(doy_grid)
        scab = smoothn(np.array(cabi), W=slai, isrobust=True, s=1,
                        TolZ=1e-6, axis=0)[0]
        f = interp1d(doys, cbrown, axis=0, bounds_error=False)                                        
        cbrowni = f(doy_grid)
        scbrown = smoothn(np.array(cbrowni) * slai, W=slai, isrobust=True, s=1,
                    TolZ=1e-6, axis=0)[0] / slai
        # Could also set them to nan
        
        slai[:, mask] = 0
        scab[:, mask] = 0
        scbrown[:, mask] = 0
        return (slai, scab, scbrown)



if __name__ == "__main__":
    import pkgutil
    from io import BytesIO

    nn_inverter = pkgutil.get_data("kaska",
                    "inverters/prosail_2NN.npz")
    approx_inverter = BytesIO(pkgutil.get_data("kaska",
                    "inverters/Prosail_5_paras.h5"))

    state_mask = "/home/ucfajlg/Data/python/KaFKA_Validation/LMU/carto/ESU.tif"
    nc_file = "/data/selene/ucfajlg/ELBARA_LMU/mirror_ftp/141.84.52.201/S1/S1_LMU_site_2017_new.nc"
    start_date = dt.datetime(2017, 5, 1)
    end_date = dt.datetime(2017, 9, 1)
    temporal_grid_space = 5
    temporal_grid = define_temporal_grid(start_date, end_date,
                                        temporal_grid_space)
    s2_obs = Sentinel2Observations(
        "/home/ucfajlg/Data/python/KaFKA_Validation/LMU/s2_obs/",
        BytesIO(nn_inverter),
        state_mask,
        band_prob_threshold=20,
        chunk=None,
        time_grid=temporal_grid)

    s1_obs = Sentinel1Observations(nc_file,
                state_mask,
                time_grid=temporal_grid)
    
    kaska = KaSKA(s2_obs, temporal_grid, state_mask, approx_inverter,
                     "/tmp/")
    slai, scab, scbrown = kaska.run_retrieval()


    np.savez("temporary_dump.npz", slai=slai, scab=scab,
     scbrown=scbrown, temporal_grid=temporal_grid) 