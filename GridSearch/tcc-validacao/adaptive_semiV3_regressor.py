import numpy as np

import xgboost as xgb
import random

from skmultiflow.core.base import BaseSKMObject, ClassifierMixin, RegressorMixin
from skmultiflow.utils import get_dimensions
from sklearn.neighbors import KNeighborsRegressor
from timeit import default_timer as timer
import os
from skmultiflow.drift_detection import ADWIN
from skmultiflow.drift_detection import KSWIN
from skmultiflow.drift_detection import DDM
from skmultiflow.drift_detection import PageHinkley

from skmultiflow.trees import HoeffdingTreeRegressor

class AdaptiveSemiRegressorJ(BaseSKMObject, RegressorMixin):

    def __init__(self,
                 learning_rate=0.3,
                 max_depth=6,
                 max_window_size=1000,
                 min_window_size=None,
                 ratio_unsampled=0,
                 small_window_size=0,
                 max_buffer=5,
                 pre_train=15,
                 detect_drift = True,
                 unic="S", 
                 adwin_delta=0.002, 
                 kswin_alpha=0.005,
                 kswin_window_size=100,
                 kswin_stat_size=30,
                 ddm_min_num_instances = 30,
                 ddm_warning_level = 2,
                 ddm_out_control_level = 3):
        super().__init__()
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.max_window_size = max_window_size
        self.min_window_size = min_window_size
        self._first_run = True
        self._booster = None
        self._temp_booster = None
        self._drift_detector = None
        self._X_buffer = np.array([])
        self._y_buffer = np.array([])
        self.adwin_delta = adwin_delta
        self.kswin_alpha = kswin_alpha
        self.kswin_window_size = kswin_window_size
        self.kswin_stat_size = kswin_stat_size
        self.ddm_min_num_instances = ddm_min_num_instances
        self.ddm_warning_level = ddm_warning_level
        self.ddm_out_control_level = ddm_out_control_level

        self._max_buffer = 25
        self._pre_train = 15

        self._inside_pre_train = self._max_buffer - self._pre_train

        self._ratio_unsampled = 0
        self._X_small_buffer = np.array([])
        self._y_small_buffer = np.array([])
        self._samples_seen = 0
        self.detect_drift = detect_drift
        self.unic = unic
        self._drift_detector = []
        self._model_idx = 0
        self._small_window_size = 150
        self._count_buffer = 0
        self._main_model = "model"
        self._temp_model = "temp"
        self._contADWIN = 0
        self._contKSWIN = 0
        self._contDDM   = 0

        self._configure()

    def _configure(self):
        self._reset_window_size()
        self._init_margin = 0.0
        self._boosting_params = {
            "objective": "reg:squarederror",
            "eta": self.learning_rate,
            "eval_metric": "rmse",
            "max_depth": self.max_depth}

        # Array de tecnicas (KSWIN, ADWIN, DDM)
        if self.detect_drift:
            if self.unic == "S":
                self._drift_detector.append(ADWIN())
            else:
                self._drift_detector.append(ADWIN(delta=self.adwin_delta))
                self._drift_detector.append(KSWIN(alpha=self.kswin_alpha, window_size=self.kswin_window_size, stat_size=self.kswin_stat_size))
                self._drift_detector.append(DDM(min_num_instances=self.ddm_min_num_instances, warning_level=self.ddm_warning_level, out_control_level=self.ddm_out_control_level))


    def reset(self):
        self._first_run = True
        self._configure()

    def partial_fit(self, X, y, classes=None, sample_weight=None):
        """
        Partially (incrementally) fit the model.

        Parameters
        ----------
        X: numpy.ndarray
            An array of shape (n_samples, n_features) with the data upon which
            the algorithm will create its model.

        y: Array-like
            An array of shape (, n_samples) containing the classification
            targets for all samples in X. Only binary data is supported.

        classes: Not used.

        sample_weight: Not used.

        Returns
        -------
        AdaptiveXGBoostClassifier
            self
        """
        for i in range(X.shape[0]):
            self._partial_fit(np.array([X[i, :]]), np.array([y[i]]))
        return self

    def _change_small_window(self, npArrX, npArrY):
        if npArrX.shape[0] < self._small_window_size:
            sizeToRemove = 0
            nextSize = self._X_small_buffer.shape[0] + npArrX.shape[0]
            if nextSize > self._small_window_size:
                sizeToRemove = nextSize - self._small_window_size
            #deleta os dados velhos
            delete_idx = [i for i in range(sizeToRemove)]

            if len(delete_idx) > 0:
                self._X_small_buffer = np.delete(self._X_small_buffer, delete_idx, axis=0)
                self._y_small_buffer = np.delete(self._y_small_buffer, delete_idx, axis=0)
            
            self._X_small_buffer = np.concatenate((self._X_small_buffer, npArrX))
            self._y_small_buffer = np.concatenate((self._y_small_buffer, npArrY))
        else:
            self._X_small_buffer = npArrX[0:self._small_window_size]
            self._y_small_buffer = npArrY[0:self._small_window_size]

    def _unlabeled_fit(self):
        # unlabeled = map(lambda x: x != 0 and x != 1, self._y_buffer)
        npArrX = []
        npArrY = []

        unlabeled = []
        labeledX = []
        labeledY = []
        for i in range(len(self._X_buffer)):
            currentY = self._y_buffer[i]

            max_size = int(self._ratio_unsampled * len(self._X_buffer))
            # print(max_size)
            if max_size > i:
                unlabeled.append(self._X_buffer[i])
            else:
                labeledX.append(self._X_buffer[i])
                labeledY.append(currentY)
            # if currentY != 1 and currentY != 0:
            #     unlabeled.append(self._X_buffer[i])
            # else:
            #     labeledX.append(self._X_buffer[i])
            #     labeledY.append(currentY)
        npArrX = np.array(labeledX)
        npArrY = np.array(labeledY)
        if npArrX.shape[0] > 0:
            self._change_small_window(npArrX, npArrY)
        npUnlabeled = np.array(unlabeled)
        if npArrX.shape[0] > 6:
            if npUnlabeled.shape[0] > 0:
                nbrs = KNeighborsRegressor(n_neighbors=3, algorithm='ball_tree').fit(self._X_small_buffer, self._y_small_buffer)

                proba = nbrs.predict_proba(npUnlabeled)

                for j in range(len(proba)):
                    biggerIndex = np.argmax(proba[j])
                    otherIndex = biggerIndex == 0 and 1 or 0
                    margim = proba[j][biggerIndex] - proba[j][otherIndex]

                    if (margim > 0.5):
                        npArrXNew = np.array([npUnlabeled[j]])
                        npArrYNew = np.array([biggerIndex])
                        npArrX = np.concatenate((npArrX, npArrXNew))
                        npArrY = np.concatenate((npArrY, npArrYNew))
        # print("semi")
        # print(len(npArrX))
        # print(len(self._X_small_buffer))
        return (npArrX, npArrY)


    def _partial_fit(self, X, y):
        if self._first_run:
            self._X_buffer = np.array([]).reshape(0, get_dimensions(X)[1])
            self._y_buffer = np.array([])
            self._X_small_buffer = np.array([]).reshape(0, get_dimensions(X)[1])
            self._y_small_buffer = np.array([])
            self._first_run = False
        self._X_buffer = np.concatenate((self._X_buffer, X))
        self._y_buffer = np.concatenate((self._y_buffer, y))

        while self._X_buffer.shape[0] >= self.window_size:
            self._count_buffer = self._count_buffer + 1
            npArrX, npArrY = self._unlabeled_fit()
            if npArrX.shape[0] > 0:
                self._train_on_mini_batch(X=npArrX,
                                        y=npArrY)
                                    
            delete_idx = [i for i in range(self.window_size)]
            self._X_buffer = np.delete(self._X_buffer, delete_idx, axis=0)
            self._y_buffer = np.delete(self._y_buffer, delete_idx, axis=0)

            # Check window size and adjust it if necessary
            self._adjust_window_size()
            
        # Support for concept drift    
        if self.detect_drift:
            error = abs (self.predict(X) - y)            
            for detector in self._drift_detector:
                detector.add_element(float(error))
                if (detector.detected_change()):
                    self._reset_window_size()
                    break
        
        # # Support for concept drift
        # if self.detect_drift:
        #     correctly_classifies = abs (self.predict(X) - y.astype(int))
        #     if self._drift_detector[0]:
        #         # Check for warning
        #         self._drift_detector[0].add_element(int(correctly_classifies))
        #         # Check if there was a change
        #         if self._drift_detector[0].detected_change():
                    
        #             # Reset window size
        #             self._reset_window_size()
                    
        #             # self._count_buffer = 0

        #             # booster = self._temp_booster
        #             # # Update ensemble
        #             # self._booster = booster

        #     if self._drift_detector[1]:
        #         # Check for warning
        #         self._drift_detector[1].add_element(int(correctly_classifies))
        #         # Check if there was a change
        #         if self._drift_detector[1].detected_change():
        #             # Reset window size
        #             self._reset_window_size()

        #             # self._count_buffer = 0
                    
        #             # booster = self._temp_booster
        #             # # Update ensemble
        #             # self._booster = booster

        #     if self._drift_detector[2]:
        #         # Check for warning
        #         self._drift_detector[2].add_element(int(correctly_classifies))
        #         # Check if there was a change
        #         if self._drift_detector[2].detected_change():
        #             # Reset window size
        #             self._reset_window_size()

        #             # self._count_buffer = 0
                    
        #             # booster = self._temp_booster
        #             # # Update ensemble
        #             # self._booster = booster

    def _adjust_window_size(self):
        if self._dynamic_window_size < self.max_window_size:
            self._dynamic_window_size *= 2
            if self._dynamic_window_size > self.max_window_size:
                self.window_size = self.max_window_size
            else:
                self.window_size = self._dynamic_window_size

    def _reset_window_size(self):
        if self.min_window_size:
            self._dynamic_window_size = self.min_window_size
        else:
            self._dynamic_window_size = self.max_window_size
        self.window_size = self._dynamic_window_size

    def _train_on_mini_batch(self, X, y):
        
        if self._count_buffer >= self._inside_pre_train:
            temp_booster = self._train_booster(X, y, self._temp_model, self._temp_booster)
            self._temp_booster = temp_booster

        if self._count_buffer >= self._max_buffer:
            booster = self._temp_booster
            self._temp_booster = None
            self._count_buffer = 0
            self._temp_model,self._main_model = self._main_model,self._temp_model
        else:
            booster = self._train_booster(X, y, self._main_model, self._booster)
        
        # Update ensemble
        self._booster = booster

    def _train_booster(self, X: np.ndarray, y: np.ndarray, fileName, currentBooster):
        d_mini_batch_train = xgb.DMatrix(X, y)
        
        # teste = self._boosting_params.copy()
        # teste["num_boost_round"] = 1

        if currentBooster:
            booster = xgb.train(params=self._boosting_params,
                                dtrain=d_mini_batch_train,
                                num_boost_round=1,
                                xgb_model=fileName)
            # booster = xgb.XGBRegressor(**self._boosting_params)
            # booster.fit(X, y, xgb_model=fileName, eval_metric="rmse")
            booster.save_model(fileName)
        else:
            booster = xgb.train(params=self._boosting_params,
                                dtrain=d_mini_batch_train,
                                num_boost_round=1,
                                verbose_eval=False)
            # booster = xgb.XGBRegressor(**self._boosting_params)
            # booster.fit(X, y, verbose=False, eval_metric="rmse")
            booster.save_model(fileName)
        return booster

    def predict(self, X):
        """
        Predict the class label for sample X

        Parameters
        ----------
        X: numpy.ndarray
            An array of shape (n_samples, n_features) with the samples to
            predict the class label for.

        Returns
        -------
        numpy.ndarray
            A 1D array of shape (, n_samples), containing the
            predicted class labels for all instances in X.

        """
        #start_time = timer()
        if self._booster:
            d_test = xgb.DMatrix(X)
            predicted = self._booster.predict(d_test)
            return predicted
        # Ensemble is empty, return default values (0)
        return np.zeros(get_dimensions(X)[0])

    def predict_proba(self, X):
        """
        Not implemented for this method.
        """
        raise NotImplementedError(
            "predict_proba is not implemented for this method.")
